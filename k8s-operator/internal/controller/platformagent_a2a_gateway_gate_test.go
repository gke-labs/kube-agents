package controller

// The A2A gateway creation gate, from the side that waits on it.
//
// The gate withholds the gateway Deployment's CREATE until one auth callout
// replica is both ready and on the current pod template
// (a2aCalloutCanServeANewGateway) - a gateway that already exists keeps
// reconciling through a callout outage, because withholding its updates would
// freeze its image and env at whatever the outage interrupted, and because the
// sessions it already spawned hang off its Deployment UID. The gate used to
// read BusCredentialsReady, whose all-replicas rule held a first gateway for as
// long as a second callout pod could not schedule; it now computes its own
// answer from the callout Deployment, and the two deliberately answer different
// questions. These tests pin both the release and the cases it must not admit.

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
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

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
	dep.Status.ObservedGeneration = dep.Generation
	dep.Status.Replicas = replicas
	dep.Status.ReadyReplicas = replicas
	dep.Status.UpdatedReplicas = replicas
	if err := cl.Status().Update(ctx, dep); err != nil {
		t.Fatalf("update callout status: %v", err)
	}
}

// reportCalloutStatus puts the callout Deployment into an arbitrary counted
// state, with the Deployment controller reported as having seen the current
// spec, so the gate's answer is about the counts and nothing else. The tests
// below use it for the states a real callout passes through that
// reportCalloutServing does not model: a second pod Pending, a wedged roll, a
// reap window.
func reportCalloutStatus(t *testing.T, ctx context.Context, cl client.Client, agent *agentv1alpha1.PlatformAgent, replicas, ready, updated int32) {
	t.Helper()
	dep := &appsv1.Deployment{}
	key := types.NamespacedName{Name: a2aCalloutName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, key, dep); err != nil {
		t.Fatalf("get callout Deployment: %v", err)
	}
	dep.Status.ObservedGeneration = dep.Generation
	dep.Status.Replicas = replicas
	dep.Status.ReadyReplicas = ready
	dep.Status.UpdatedReplicas = updated
	if err := cl.Status().Update(ctx, dep); err != nil {
		t.Fatalf("update callout status: %v", err)
	}
}

// letTheGatewayThrough is reportCalloutServing plus two passes. The gate reads
// the callout Deployment itself, so the first pass after the status lands is
// the one that creates the gateway; the second is the settled pass, where the
// deferred BusCredentialsReady write has landed and the phase has been computed
// with the gateway in the namespace, which is the state tests that are about
// something other than the gate want to read from.
func letTheGatewayThrough(t *testing.T, ctx context.Context, cl client.Client, r *PlatformAgentReconciler, req ctrl.Request, agent *agentv1alpha1.PlatformAgent) {
	t.Helper()
	reportCalloutServing(t, ctx, cl, agent)
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d after the callout came up: %v", i+1, err)
		}
	}
}

// theCalloutIsServing renders the A2A stack once and reports its callout
// Deployment serving, which is the one thing the gateway gate reads. It is for
// tests that drive reconcileA2A directly instead of going through Reconcile:
// the fake client runs no Deployment controller, so on that path the callout
// the first pass renders never reports a replica, the gate holds forever, and
// the gateway Deployment is never created. An assertion about a gateway that
// was never created passes on absence, which is the failure mode this helper
// exists to keep out of the suite -- so call it, and then assert the Deployment
// is there before asserting anything else about it.
func theCalloutIsServing(t *testing.T, ctx context.Context, cl client.Client, r *PlatformAgentReconciler, agent *agentv1alpha1.PlatformAgent) {
	t.Helper()
	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatalf("render the A2A stack so the callout exists to report on: %v", err)
	}
	reportCalloutServing(t, ctx, cl, agent)
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
		WithObjects(append([]client.Object{agent, sandboxKeysSecret(agent), discordBotSecret(agent)}, extra...)...).
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
		t.Fatalf("gateway rendered while no callout replica serves (err=%v); it would dispatch onto a bus that refuses every session's connect", err)
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
	// Two passes to land it. The gate reads the Deployment, so it sees the
	// outage on the first; the condition is published on the way out, and
	// the second pass is what makes it readable below. The spec change must
	// arrive when the gate is genuinely reading an outage, or this test passes
	// on staleness rather than on the rule it is about, and the condition is
	// the independent witness that the outage landed on the cluster.
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

// The release: one replica ready on the current template is enough for a new
// gateway, and BusCredentialsReady is not consulted.
//
// The callout's replicas join a NATS queue group (a2a/authcallout/service.go,
// AuthQueueGroup) precisely so that exactly one of them answers each
// authorization request, so ONE ready replica already mints credentials for
// every session. The condition's all-replicas rule was written for a different
// question -- "is the callout as a whole serving the map this render names",
// where a half-rolled Deployment genuinely is not -- and the gate used to
// import it wholesale, which held a NEW install's gateway for as long as a
// second replica could not schedule (node pressure, a namespace quota, an image
// pull that fails on one node), on a bus that would have authenticated every
// one of its sessions.
//
// This is the shape a Pending second pod has in Deployment status: both pods
// created on the current template (Updated=2, Replicas=2), one of them ready.
// The condition is asserted False in the same breath, because the two now
// disagree on purpose and a test that only checked the gateway would pass just
// as well on a loosened condition, which is not the change.
func TestAPartlyReadyCalloutLetsANewGatewayThrough(t *testing.T) {
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

	// Every pod on the current template, one of them ready: the second is
	// Pending. The only thing short of the condition's bar is that pod.
	reportCalloutStatus(t, ctx, cl, agent, rendered, 1, rendered)

	// Two passes: the first is the one the gate decides on, and the second
	// lands the deferred condition write so it can be read beside the
	// gateway. The Job is kept complete across them so neither pass is
	// provisioning's.
	for i := 0; i < 2; i++ {
		completeTheProvisionJob(t, ctx, cl, agent)
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d at one ready replica: %v", i+1, err)
		}
	}

	gwKey := types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, gwKey, &appsv1.Deployment{}); err != nil {
		t.Errorf("the A2A gateway is withheld (err=%v) with one of %d callout replicas ready on the current template; one serving replica answers every authorization through the queue group, and the gate is meant to pass on it", err, rendered)
	}

	// The condition still says what it says: every replica, and there is not
	// every replica. That the gateway exists beside it is the design.
	fresh := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	cond := meta.FindStatusCondition(fresh.Status.Conditions, busCredentialsReadyCondition)
	if cond == nil || cond.Status != metav1.ConditionFalse {
		t.Errorf("BusCredentialsReady = %+v at one of %d replicas ready, want False; the gate's release must not have come from loosening the condition, which is a claim about the callout as a whole", cond, rendered)
	}
}

// The case the release must not admit, and the reason a plain "one ready
// replica" rule was never taken.
//
// Under MaxUnavailable 0 a roll wedged on a pod too old to parse the rendered
// identity map keeps both old pods ready and serving the previous map while
// the new pod never becomes ready: Ready=2, Updated=1, Replicas=3. A rule that
// read ReadyReplicas alone would let a first gateway through against a callout
// no replica of which accepted the current map, and the sessions it spawned
// would be authorized against the map before it. The lower bound reads
// 2 + 1 - 3 = 0 here, and the gateway stays withheld with the requeue that
// converges it once the roll unwedges.
func TestAWedgedCalloutRollStillWithholdsANewGateway(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	ctx := context.Background()

	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}
	reportCalloutStatus(t, ctx, cl, agent, 3, 2, 1)

	completeTheProvisionJob(t, ctx, cl, agent)
	res, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile on the wedged roll: %v", err)
	}

	gwKey := types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, gwKey, &appsv1.Deployment{}); !errors.IsNotFound(err) {
		t.Errorf("the A2A gateway exists (err=%v) on a callout at Ready=2, Updated=1, Replicas=3; both ready replicas are on the previous template, so a gateway created now spawns sessions authorized against the previous identity map", err)
	}
	if res.RequeueAfter != 30*time.Second {
		t.Errorf("a gateway held on a wedged roll requeued after %s, want 30s", res.RequeueAfter)
	}
}

// The rule's one error direction, through Reconcile: a terminated pod still
// counted in Status.Replicas makes one ready updated replica read 1 + 1 - 2 = 0,
// so the gate holds for that pass, and passes as soon as the count drops. The
// false negative costs a held pass and a requeue, never a wrong gateway.
func TestAReapWindowHoldsTheGatewayForOnePass(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	ctx := context.Background()

	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}
	gwKey := types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}

	// One replica ready and updated, one terminating pod still counted.
	reportCalloutStatus(t, ctx, cl, agent, 2, 1, 1)
	completeTheProvisionJob(t, ctx, cl, agent)
	res, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile in the reap window: %v", err)
	}
	if err := cl.Get(ctx, gwKey, &appsv1.Deployment{}); !errors.IsNotFound(err) {
		t.Fatalf("the A2A gateway exists (err=%v) at Ready=1, Updated=1, Replicas=2; status cannot tell this apart from one old ready pod beside one new unready one, so the gate has to hold", err)
	}
	if res.RequeueAfter != 30*time.Second {
		t.Errorf("a gateway held in the reap window requeued after %s, want 30s; the requeue is what turns the false negative into a delay", res.RequeueAfter)
	}

	// The terminated pod is reaped: the count drops, and nothing else changes.
	reportCalloutStatus(t, ctx, cl, agent, 1, 1, 1)
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile after the reap: %v", err)
	}
	if err := cl.Get(ctx, gwKey, &appsv1.Deployment{}); err != nil {
		t.Errorf("the A2A gateway is still withheld (err=%v) at Ready=1, Updated=1, Replicas=1; the one replica is both ready and current, and the hold was meant to last one pass", err)
	}
}

// The read behind the rule, through Reconcile: the gate reads the callout from
// the informer after the same pass applied it, and the informer learns of the
// apply by watch event, so on the pass that changes the callout's pod template
// it can still hold the object from before the write. That copy is not merely
// late. It carries the previous Generation with ObservedGeneration equal to it
// and both replicas ready and updated on the template the apply just replaced,
// so the counts read serving and the ObservedGeneration guard cannot tell. The
// apply's response carries the Generation the write produced, and the gate
// refuses a copy that has not reached it.
//
// The fake client has read-your-writes, so the staleness is staged: the apply
// of the callout reports the new Generation on the object it returns, as the
// API server does, while every Get of the callout answers with the previous
// one until the test lets the "informer" catch up.
type staleCalloutInformer struct {
	callout types.NamespacedName
	// The Generation the apply reports, and the one the Get still answers.
	applied, cached int64
	// The counts the cached copy carries: fully serving, on the old template.
	replicas int32
}

func (f *staleCalloutInformer) isCallout(obj client.Object) bool {
	_, isDeployment := obj.(*appsv1.Deployment)
	return isDeployment && client.ObjectKeyFromObject(obj) == f.callout
}

func (f *staleCalloutInformer) funcs() interceptor.Funcs {
	ssa := fakeServerSideApplyInterceptors().Patch
	return interceptor.Funcs{
		Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if err := ssa(ctx, cl, obj, patch, opts...); err != nil {
				return err
			}
			if f.isCallout(obj) && patch.Type() == types.ApplyPatchType {
				// What the server's response says after the write; the
				// fake's stored copy is what the Get below reads.
				obj.(*appsv1.Deployment).Generation = f.applied
			}
			return nil
		},
		Get: func(ctx context.Context, cl client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if err := cl.Get(ctx, key, obj, opts...); err != nil {
				return err
			}
			if f.isCallout(obj) {
				dep := obj.(*appsv1.Deployment)
				dep.Generation = f.cached
				dep.Status.ObservedGeneration = f.cached
				dep.Status.Replicas = f.replicas
				dep.Status.ReadyReplicas = f.replicas
				dep.Status.UpdatedReplicas = f.replicas
			}
			return nil
		},
	}
}

func TestAnInformerCopyOlderThanThisPassesApplyHoldsTheGateway(t *testing.T) {
	agent := a2aTestAgent()
	stale := &staleCalloutInformer{
		callout:  types.NamespacedName{Name: a2aCalloutName(agent), Namespace: agent.Namespace},
		applied:  2,
		cached:   1,
		replicas: 2,
	}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, sandboxKeysSecret(agent), discordBotSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(stale.funcs()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()

	// Two passes: the first renders the stack, and on both the informer
	// answers with a callout at Generation 1, fully serving, while the apply
	// has just produced Generation 2. Without the Generation check the
	// counts alone (2 + 2 - 2 = 2) let the gateway through on the first.
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d on the stale copy: %v", i+1, err)
		}
	}
	completeTheProvisionJob(t, ctx, cl, agent)
	res, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile on the stale copy with provisioning complete: %v", err)
	}
	gwKey := types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, gwKey, &appsv1.Deployment{}); !errors.IsNotFound(err) {
		t.Fatalf("the A2A gateway exists (err=%v) on a callout copy at Generation 1 while this pass's apply returned Generation 2; every replica that copy counts is on the template the apply replaced", err)
	}
	if res.RequeueAfter != 30*time.Second {
		t.Errorf("a gateway held on a stale callout copy requeued after %s, want 30s", res.RequeueAfter)
	}

	// The informer delivers the write: the same object at Generation 2,
	// observed, with the second replica still coming up on the new template.
	stale.cached = 2
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile once the informer caught up: %v", err)
	}
	if err := cl.Get(ctx, gwKey, &appsv1.Deployment{}); err != nil {
		t.Errorf("the A2A gateway is still withheld (err=%v) on a callout copy at the applied Generation with every replica ready and updated; the hold was meant to last until the informer caught up", err)
	}
}

// The predicate on its own, over the Deployment states the tests above drive
// through Reconcile and the ones they do not. applied is the Generation the
// pass's own apply of the callout returned; 0 is a caller with no apply in
// hand, and the rows that carry it are about the counts alone.
func TestACalloutCanServeANewGatewayFromOneReadyUpdatedReplica(t *testing.T) {
	for _, tc := range []struct {
		name                          string
		generation, observed, applied int64
		replicas, ready, updated      int32
		want                          bool
	}{
		{name: "fully serving", generation: 1, observed: 1, replicas: 2, ready: 2, updated: 2, want: true},
		{name: "second replica Pending", generation: 1, observed: 1, replicas: 2, ready: 1, updated: 2, want: true},
		{name: "wedged roll under MaxUnavailable 0", generation: 1, observed: 1, replicas: 3, ready: 2, updated: 1, want: false},
		{name: "healthy roll, surge pod ready", generation: 1, observed: 1, replicas: 3, ready: 3, updated: 1, want: true},
		{name: "reap window, terminated pod still counted", generation: 1, observed: 1, replicas: 2, ready: 1, updated: 1, want: false},
		{name: "reap window closed", generation: 1, observed: 1, replicas: 1, ready: 1, updated: 1, want: true},
		{name: "one ready, none on the current template", generation: 1, observed: 1, replicas: 1, ready: 1, updated: 0, want: false},
		{name: "outage, both pods present", generation: 1, observed: 1, replicas: 2, ready: 0, updated: 0, want: false},
		{name: "no status yet", generation: 0, observed: 0, replicas: 0, ready: 0, updated: 0, want: false},
		{name: "spec change not yet observed", generation: 2, observed: 1, replicas: 2, ready: 2, updated: 2, want: false},
		{name: "informer copy is the one this pass applied", generation: 2, observed: 2, applied: 2, replicas: 2, ready: 1, updated: 2, want: true},
		{name: "informer copy predates this pass's apply", generation: 1, observed: 1, applied: 2, replicas: 2, ready: 2, updated: 2, want: false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dep := &appsv1.Deployment{
				ObjectMeta: metav1.ObjectMeta{Generation: tc.generation},
				Status: appsv1.DeploymentStatus{
					ObservedGeneration: tc.observed,
					Replicas:           tc.replicas,
					ReadyReplicas:      tc.ready,
					UpdatedReplicas:    tc.updated,
				},
			}
			if got := a2aCalloutCanServeANewGateway(dep, tc.applied); got != tc.want {
				t.Errorf("a2aCalloutCanServeANewGateway(gen %d observed %d, replicas %d ready %d updated %d; applied %d) = %v, want %v",
					tc.generation, tc.observed, tc.replicas, tc.ready, tc.updated, tc.applied, got, tc.want)
			}
		})
	}
}

// ---- the backend gate (#1660, option 1) -----------------------------------
//
// The gateway binary refuses to start without a chat backend, so a next
// install with no discord-bot Secret and no door armed used to render a
// Deployment that crash-looped forever. The render now asks first: no
// backend, no first creation, and the CR says why. Creation only, like the
// callout gate: a gateway that exists is reconciled whatever happened to its
// backend.

// a2aGateTestReconcilerWithoutABackend is a2aGateTestReconciler minus the
// discord-bot Secret it seeds, for the tests that are about its absence.
func a2aGateTestReconcilerWithoutABackend(t *testing.T, agent *agentv1alpha1.PlatformAgent) (*PlatformAgentReconciler, client.Client, ctrl.Request) {
	t.Helper()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, sandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	return &PlatformAgentReconciler{Client: cl, Scheme: scheme}, cl,
		ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
}

// TestADarkGatewayKeepsTheReconcileRequeuing: the discord-bot Secret is not
// watched, so the pass that renders the gateway once a Secret appears has to
// be a pass that happens. Measured on a provisioned bus with the callout
// serving, for the reason TestAWithheldGatewayRendersOnceACalloutReplicaServes
// gives: an unprovisioned bus or a held gateway requeues on its own account,
// and the install where the dark term alone carries the requeue is the one
// where everything else is done. The interval, not merely non-zero: the
// telemetry re-probe requeues at 15 minutes, which is how long a new Secret
// would wait with this term deleted.
func TestADarkGatewayKeepsTheReconcileRequeuing(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconcilerWithoutABackend(t, agent)
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	completeTheProvisionJob(t, ctx, cl, agent)

	// Settled first: a first pass over a fresh CR returns on its own
	// bookkeeping (the finalizer write) before the requeue is decided, as
	// the callout gate's test does.
	var res ctrl.Result
	for i := 0; i < 3; i++ {
		var err error
		if res, err = r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d with a dark gateway: %v", i+1, err)
		}
	}
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}, &appsv1.Deployment{}); !errors.IsNotFound(err) {
		t.Fatalf("precondition: the gateway rendered without a backend (err=%v)", err)
	}
	if res.RequeueAfter != 30*time.Second {
		t.Errorf("a dark gateway on a provisioned bus requeued after %s, want 30s; nothing watches the discord-bot Secret", res.RequeueAfter)
	}
}

// TestARunningGatewayDoesNotReadTheSecret: the backend question costs an
// uncached Secret read, and it is asked only on the pass that would create
// the gateway. An install whose gateway exists pays nothing for the gate.
func TestARunningGatewayDoesNotReadTheSecret(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	agent := a2aTestAgent()
	scheme := setupScheme()
	secretReads := 0
	funcs := fakeServerSideApplyInterceptors()
	funcs.Get = func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
		if _, ok := obj.(*corev1.Secret); ok && key.Name == a2aDiscordBotSecretName {
			secretReads++
		}
		return c.Get(ctx, key, obj, opts...)
	}
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, sandboxKeysSecret(agent), discordBotSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(funcs).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatal(err)
	}
	key := types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, key, &appsv1.Deployment{}); err != nil {
		t.Fatalf("precondition: the gateway renders with the Secret present: %v", err)
	}
	if secretReads == 0 {
		t.Fatal("precondition: the creating pass never asked the backend question; the counter is not counting")
	}

	secretReads = 0
	for i := 0; i < 3; i++ {
		if _, err := r.reconcileA2A(ctx, agent); err != nil {
			t.Fatalf("reconcileA2A %d with the gateway running: %v", i+1, err)
		}
	}
	if secretReads != 0 {
		t.Errorf("a running gateway's reconcile read the discord-bot Secret %d times in three passes, want 0", secretReads)
	}
}

func TestAGatewayIsNotRenderedWithoutAChatBackend(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	agent := a2aTestAgent()
	r, cl, _ := a2aGateTestReconcilerWithoutABackend(t, agent)
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)

	state, err := r.reconcileA2A(ctx, agent)
	if err != nil {
		t.Fatalf("reconcileA2A: %v", err)
	}
	if !state.gatewayDark {
		t.Fatal("the render did not report the gateway withheld for want of a backend")
	}
	if !strings.Contains(state.gatewayDarkReason, a2aDiscordBotSecretName) || !strings.Contains(state.gatewayDarkReason, a2aInjectBackendEnvVar) {
		t.Errorf("the reason does not name what would render the gateway: %q", state.gatewayDarkReason)
	}
	if state.gatewayHeld {
		t.Error("the callout gate was consulted for a gateway that is dark; the backend question comes first")
	}
	err = cl.Get(ctx, types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}, &appsv1.Deployment{})
	if !errors.IsNotFound(err) {
		t.Fatalf("a gateway Deployment exists with no backend to start on (err=%v)", err)
	}
}

func TestTheDiscordSecretRendersTheGateway(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	agent := a2aTestAgent()
	r, cl, _ := a2aGateTestReconcilerWithoutABackend(t, agent)
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	if state, err := r.reconcileA2A(ctx, agent); err != nil || !state.gatewayDark {
		t.Fatalf("precondition: want a dark gateway before the Secret exists (state=%+v err=%v)", state, err)
	}

	if err := cl.Create(ctx, discordBotSecret(agent)); err != nil {
		t.Fatal(err)
	}
	state, err := r.reconcileA2A(ctx, agent)
	if err != nil {
		t.Fatalf("reconcileA2A after the Secret: %v", err)
	}
	if state.gatewayDark {
		t.Fatal("the gateway is still reported dark with the discord-bot Secret present")
	}
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}, &appsv1.Deployment{}); err != nil {
		t.Fatalf("the gateway Deployment was not rendered once a backend existed: %v", err)
	}
}

func TestTheInjectDoorRendersTheGatewayWithoutASecret(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "true")
	agent := a2aTestAgent()
	r, cl, _ := a2aGateTestReconcilerWithoutABackend(t, agent)
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	state, err := r.reconcileA2A(ctx, agent)
	if err != nil {
		t.Fatalf("reconcileA2A: %v", err)
	}
	if state.gatewayDark {
		t.Fatal("the door is armed and the gateway is reported dark; the eval install would never get a gateway")
	}
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}, &appsv1.Deployment{}); err != nil {
		t.Fatalf("the gateway Deployment was not rendered with the door armed: %v", err)
	}
}

// TestAnExistingGatewayKeepsReconcilingWithoutABackend: creation only. Taking
// the Secret away from a running gateway must not withhold its reconcile,
// because deleting the Deployment would take every session pod with it.
func TestAnExistingGatewayKeepsReconcilingWithoutABackend(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	agent := a2aTestAgent()
	r, cl, _ := a2aGateTestReconciler(t, agent)
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatal(err)
	}
	key := types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, key, &appsv1.Deployment{}); err != nil {
		t.Fatalf("precondition: the gateway renders with the Secret present: %v", err)
	}

	if err := cl.Delete(ctx, discordBotSecret(agent)); err != nil {
		t.Fatal(err)
	}
	state, err := r.reconcileA2A(ctx, agent)
	if err != nil {
		t.Fatalf("reconcileA2A after the Secret went away: %v", err)
	}
	if state.gatewayDark {
		t.Error("an existing gateway was reported dark; the rule is creation only")
	}
	if err := cl.Get(ctx, key, &appsv1.Deployment{}); err != nil {
		t.Fatalf("the existing gateway Deployment is gone: %v", err)
	}
}

// TestADiscordSecretWithoutATokenIsNotABackend: the gateway reads the `token`
// key through an optional env reference, so a Secret carrying anything else
// would render a gateway that starts with no token and exits. Withheld, and
// the reason names the key.
func TestADiscordSecretWithoutATokenIsNotABackend(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	agent := a2aTestAgent()
	r, cl, _ := a2aGateTestReconcilerWithoutABackend(t, agent)
	ctx := context.Background()
	wrong := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: a2aDiscordBotSecretName, Namespace: agent.Namespace},
		Data:       map[string][]byte{"DISCORD_TOKEN": []byte("misnamed")},
	}
	if err := cl.Create(ctx, wrong); err != nil {
		t.Fatal(err)
	}
	theCalloutIsServing(t, ctx, cl, r, agent)
	state, err := r.reconcileA2A(ctx, agent)
	if err != nil {
		t.Fatalf("reconcileA2A: %v", err)
	}
	if !state.gatewayDark {
		t.Fatal("a discord-bot Secret with no token key counted as a backend; the gateway would render and crash-loop")
	}
	if !strings.Contains(state.gatewayDarkReason, a2aDiscordBotTokenKey) {
		t.Errorf("the reason does not name the missing key: %q", state.gatewayDarkReason)
	}
}
