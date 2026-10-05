package controller

import (
	"context"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/api/meta"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// A verifier that is restarting before the provision Job has created the cap
// bucket is the a2a stack coming up, not a fault. The verifier waits in-process
// for the bus and for the bucket itself (a2a/cmd/verifier's bindStore), so what
// reaches this window is the dependency it cannot wait out: the callout is
// applied one step ahead of it and takes its own moment to serve, and a bus
// that refuses the same identity twice aborts nats.go's reconnect loop for
// good, which the verifier answers by exiting so the pod restarts.
//
// The pod scan has to tell that from a fault, because the arm of
// updateStatusReady that calls the scan is live for exactly that window
// (notReady holds "bus provisioning" until the same Job completes). Reporting
// it would turn a fresh `next` install's ordinary Provisioning into a Degraded
// an operator acts on, for a stack that is merely not up yet.
//
// The suppression is narrow in three directions and each gets a case here: the
// container, the reason, and the bucket not yet existing.

func bucketWaitAgent() *agentv1alpha1.PlatformAgent {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}
	agent.Spec.Mode = ptr.To(string(ModeNext))
	return agent
}

func verifierPod(agent *agentv1alpha1.PlatformAgent, container, waitingReason string) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aVerifierName(agent) + "-abc123",
			Namespace: agent.Namespace,
			Labels:    map[string]string{"app": a2aVerifierName(agent)},
		},
		Status: corev1.PodStatus{
			ContainerStatuses: []corev1.ContainerStatus{{
				Name:  container,
				State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: waitingReason}},
			}},
			Conditions: []corev1.PodCondition{{Type: corev1.PodScheduled, Status: corev1.ConditionTrue}},
		},
	}
}

func TestTheVerifiersBucketWaitIsNotReportedAsADegradedInstall(t *testing.T) {
	cases := []struct {
		name string
		// capBucketProvisioned is the provision Job having completed.
		provisioned bool
		container   string
		reason      string
		wantPhase   string
		wantReason  string
	}{
		{
			// The case the whole change exists for: every fresh next install.
			name:        "a crash loop before the bucket exists is the design, not a fault",
			provisioned: false,
			container:   a2aVerifierContainerName,
			reason:      reasonCrashLoopBackOff,
			wantPhase:   "Provisioning",
			wantReason:  "Provisioning",
		},
		{
			// Narrow on the bucket: once the Job is done the wait is over, so a
			// crash loop is the verifier failing at something else.
			name:        "the same crash loop after the bucket exists is a fault",
			provisioned: true,
			container:   a2aVerifierContainerName,
			reason:      reasonCrashLoopBackOff,
			wantPhase:   "Degraded",
			wantReason:  reasonCrashLoopBackOff,
		},
		{
			// Narrow on the reason, and the reason this selector was added at
			// all: a2aReleaseImage can name something unpullable at any of its
			// three rungs, and a verifier that cannot start refuses every task.
			name:        "an unpullable image is a fault even before the bucket exists",
			provisioned: false,
			container:   a2aVerifierContainerName,
			reason:      "ImagePullBackOff",
			wantPhase:   "Degraded",
			wantReason:  "ImagePullBackOff",
		},
		{
			// Narrow on the container: the suppression must not swallow a fault
			// on something else sharing the verifier's pod.
			name:        "another container crash looping in the verifier pod is a fault",
			provisioned: false,
			container:   "sidecar",
			reason:      reasonCrashLoopBackOff,
			wantPhase:   "Degraded",
			wantReason:  reasonCrashLoopBackOff,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			scheme := setupScheme()
			agent := bucketWaitAgent()
			if !a2aStackRendering(agent) {
				t.Fatalf("the fixture does not render the A2A stack, so the verifier selector is never appended and every case below would pass vacuously")
			}
			pod := verifierPod(agent, tc.container, tc.reason)
			cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, pod).Build()
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

			phase, reason, message := r.getDeploymentStatusDetails(context.Background(), agent, tc.provisioned)
			if phase != tc.wantPhase {
				t.Errorf("phase = %q, want %q (message %q)", phase, tc.wantPhase, message)
			}
			if reason != tc.wantReason {
				t.Errorf("reason = %q, want %q", reason, tc.wantReason)
			}
			if tc.wantPhase == "Degraded" && !strings.Contains(message, tc.container) {
				t.Errorf("the Degraded message does not name the faulting container %q: %q", tc.container, message)
			}
		})
	}
}

// The suppression is keyed on the verifier's own label, so a crash loop
// somewhere else in the install stays reportable during the same window. Without
// this the fix would trade a false Degraded for a missing one.
//
// The label term and the container-name term are separately load-bearing, and it
// took a mutation to see it: dropping the label alone survived the first version
// of this file, because every other pod the scan reaches happens to have no
// container called "verifier". That is a fact about today's renders, not a
// property of the guard, so the second test below pins the label term against a
// pod that does.
func TestTheBucketWaitSuppressionIsKeyedOnTheVerifierAlone(t *testing.T) {
	scheme := setupScheme()
	agent := bucketWaitAgent()

	gateway := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-gateway-xyz",
			Namespace: agent.Namespace,
			Labels:    map[string]string{"app": agent.Name + "-gateway"},
		},
		Status: corev1.PodStatus{
			ContainerStatuses: []corev1.ContainerStatus{{
				Name:  "platform-agent",
				State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: reasonCrashLoopBackOff}},
			}},
			Conditions: []corev1.PodCondition{{Type: corev1.PodScheduled, Status: corev1.ConditionTrue}},
		},
	}
	// The verifier is in its by-design wait at the same time, which is the
	// ordinary shape of a fresh install that is also broken.
	verifier := verifierPod(agent, a2aVerifierContainerName, reasonCrashLoopBackOff)

	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, gateway, verifier).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	phase, reason, message := r.getDeploymentStatusDetails(context.Background(), agent, false)
	if phase != "Degraded" || reason != reasonCrashLoopBackOff {
		t.Fatalf("phase/reason = %q/%q, want Degraded/%s", phase, reason, reasonCrashLoopBackOff)
	}
	if !strings.Contains(message, "platform-agent") {
		t.Errorf("the Degraded names the wrong container; want the gateway's: %q", message)
	}
	if strings.Contains(message, a2aVerifierName(agent)) {
		t.Errorf("the Degraded names the verifier, whose crash loop is the designed bucket wait: %q", message)
	}
}

// A container named "verifier" in some other workload's pod is not the verifier,
// and its crash loop is not the bucket wait. Nothing renders such a pod today --
// which is exactly why this is pinned: the guard must hold on the label, not on
// the coincidence that the name is unique across the install's renders.
func TestAContainerNamedVerifierElsewhereIsStillAFault(t *testing.T) {
	scheme := setupScheme()
	agent := bucketWaitAgent()

	impostor := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-gateway-xyz",
			Namespace: agent.Namespace,
			Labels:    map[string]string{"app": agent.Name + "-gateway"},
		},
		Status: corev1.PodStatus{
			ContainerStatuses: []corev1.ContainerStatus{{
				Name:  a2aVerifierContainerName,
				State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: reasonCrashLoopBackOff}},
			}},
			Conditions: []corev1.PodCondition{{Type: corev1.PodScheduled, Status: corev1.ConditionTrue}},
		},
	}

	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, impostor).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	phase, reason, message := r.getDeploymentStatusDetails(context.Background(), agent, false)
	if phase != "Degraded" || reason != reasonCrashLoopBackOff {
		t.Fatalf("phase/reason = %q/%q, want Degraded/%s (message %q)", phase, reason, reasonCrashLoopBackOff, message)
	}
	if !strings.Contains(message, impostor.Name) {
		t.Errorf("the Degraded does not name the faulting pod: %q", message)
	}
}

// The cases above inject capBucketProvisioned straight into the scan, so every
// one of them would still pass if the caller handed it the wrong reading. The
// caller's choice of input is a separate thing to get right, and this pins it.
//
// "Has the bucket been created" and "did this pass watch the Job finish" are
// not the same question. The provision Job carries a 24h
// TTLSecondsAfterFinished and a name digested from its own spec, so a settled
// install loses it daily and re-renders it on any operator upgrade that moves
// the digest. Across that re-run a2a.done is false while the bucket it created
// is still there. A verifier crash loop in that window is the only kind left
// since the bind wait moved in-process -- a real fault — and keying the
// suppression on this pass's Job status would file it as Provisioning for as
// long as the re-run takes, with every submission refused the whole time.
func TestAVerifierCrashLoopDuringAProvisionJobReRunIsAFault(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	ctx := context.Background()

	// Settle the install far enough that the bus is provisioned once.
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}
	letTheGatewayThrough(t, ctx, cl, r, req, agent)
	completeTheProvisionJob(t, ctx, cl, agent)
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal(err)
	}
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, stored); err != nil {
		t.Fatal(err)
	}
	if !busProvisioned(stored) {
		t.Fatal("precondition: the install did not record the bus provisioned once")
	}

	// The verifier is genuinely crash looping, on the settled install.
	if err := cl.Create(ctx, verifierPod(agent, a2aVerifierContainerName, reasonCrashLoopBackOff)); err != nil {
		t.Fatalf("create the verifier pod: %v", err)
	}

	// Now move the digest, which is what a TTL sweep or an operator upgrade
	// does to the Job: a new one is rendered and this pass does not see it
	// complete.
	t.Setenv(a2aProvisionImageEnvVar, "example.com/nats-box:rerender")
	state, err := r.reconcileA2A(ctx, stored)
	if err != nil {
		t.Fatal(err)
	}
	if state.done {
		t.Fatal("precondition: the re-rendered provision Job already reads complete, so this pass is not the re-run window")
	}

	phase, err := r.updateStatusReady(ctx, stored, "", otlpSourceNone, r.resolveNetpolProfile(ctx, stored), state)
	if err != nil {
		t.Fatal(err)
	}
	ready := meta.FindStatusCondition(stored.Status.Conditions, "Ready")
	if ready == nil {
		t.Fatal("no Ready condition was written")
	}
	if phase != "Degraded" || ready.Reason != reasonCrashLoopBackOff {
		t.Errorf("phase/reason = %q/%q, want Degraded/%s: the bucket exists, so this crash loop is a fault (message %q)",
			phase, ready.Reason, reasonCrashLoopBackOff, ready.Message)
	}
	if !strings.Contains(ready.Message, a2aVerifierContainerName) {
		t.Errorf("the Degraded does not name the verifier container: %q", ready.Message)
	}
}

// The other half of the same call: on a genuinely fresh install -- no
// provisioned-once record and no completed Job -- the same crash loop still has
// to read Provisioning. Without this, keying the suppression on the sticky
// record alone (dropping the a2a.done term) would pass the test above and
// reintroduce the false Degraded the suppression exists to prevent.
func TestAVerifierCrashLoopOnAFreshInstallStillReadsProvisioning(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	ctx := context.Background()

	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}
	letTheGatewayThrough(t, ctx, cl, r, req, agent)

	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, stored); err != nil {
		t.Fatal(err)
	}
	if busProvisioned(stored) {
		t.Fatal("precondition: the install already records the bus provisioned, so this is not the fresh-install window")
	}
	if err := cl.Create(ctx, verifierPod(agent, a2aVerifierContainerName, reasonCrashLoopBackOff)); err != nil {
		t.Fatalf("create the verifier pod: %v", err)
	}

	state, err := r.reconcileA2A(ctx, stored)
	if err != nil {
		t.Fatal(err)
	}
	if state.done {
		t.Fatal("precondition: the provision Job reads complete on a fresh install")
	}

	phase, err := r.updateStatusReady(ctx, stored, "", otlpSourceNone, r.resolveNetpolProfile(ctx, stored), state)
	if err != nil {
		t.Fatal(err)
	}
	ready := meta.FindStatusCondition(stored.Status.Conditions, "Ready")
	if ready == nil {
		t.Fatal("no Ready condition was written")
	}
	if phase == "Degraded" || ready.Reason == reasonCrashLoopBackOff {
		t.Errorf("phase/reason = %q/%q, want Provisioning: the bucket does not exist yet, so this crash loop is the stack coming up (message %q)",
			phase, ready.Reason, ready.Message)
	}
}

// And the a2a.done term, which the two tests above cannot tell from the sticky
// record because on both of them the two readings agree. They disagree on
// exactly one pass: the one that first watches the Job complete, before the
// provisioned-once record has been written to the CR. The bucket exists from
// that moment, so a verifier crash loop on that pass is already a fault --
// dropping a2a.done and keying on the record alone would report the install's
// first genuine verifier failure as Provisioning for one more pass.
func TestTheFirstPassThatSeesTheJobCompleteAlreadyTreatsACrashLoopAsAFault(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	ctx := context.Background()

	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}
	letTheGatewayThrough(t, ctx, cl, r, req, agent)
	completeTheProvisionJob(t, ctx, cl, agent)

	// Deliberately no Reconcile after the completion: that is the writer of
	// the provisioned-once record, and this test is the pass before it.
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, stored); err != nil {
		t.Fatal(err)
	}
	if busProvisioned(stored) {
		t.Fatal("precondition: the provisioned-once record is already written, so the two readings agree and this test proves nothing")
	}
	if err := cl.Create(ctx, verifierPod(agent, a2aVerifierContainerName, reasonCrashLoopBackOff)); err != nil {
		t.Fatalf("create the verifier pod: %v", err)
	}

	state, err := r.reconcileA2A(ctx, stored)
	if err != nil {
		t.Fatal(err)
	}
	if !state.done {
		t.Fatal("precondition: this pass did not see the provision Job complete")
	}
	if busProvisioned(stored) {
		t.Fatal("precondition: reconcileA2A wrote the provisioned-once record, so the two readings agree by the time the scan runs")
	}

	phase, err := r.updateStatusReady(ctx, stored, "", otlpSourceNone, r.resolveNetpolProfile(ctx, stored), state)
	if err != nil {
		t.Fatal(err)
	}
	ready := meta.FindStatusCondition(stored.Status.Conditions, "Ready")
	if ready == nil {
		t.Fatal("no Ready condition was written")
	}
	if phase != "Degraded" || ready.Reason != reasonCrashLoopBackOff {
		t.Errorf("phase/reason = %q/%q, want Degraded/%s: the Job completed on this pass, so the bucket exists and the wait is over (message %q)",
			phase, ready.Reason, reasonCrashLoopBackOff, ready.Message)
	}
}
