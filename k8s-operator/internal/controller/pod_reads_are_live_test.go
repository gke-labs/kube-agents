package controller

import (
	"context"
	"fmt"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Every Pod this operator reads is read live, through podReader or through the
// identical reader() the usage poller's own file carries. Nothing in
// SetupWithManager watches Pods, so a single cached Pod read is what starts
// their informer, and that informer is cluster-wide however narrow the read
// that started it: client.InNamespace filters a cached List after the fact
// (#3493). Pods outnumber every other kind in most clusters, so one such read
// turns the operator's resident set from "the installs it runs" into "the
// cluster it runs on".
//
// The fake client cannot show that growth -- its store is already in memory --
// so these tests do what the Secret test next door does: they give the
// reconciler a "cache" that refuses Pods outright, and then assert on an
// answer only a pod the read returned can produce. Each case covers one of the
// four Pod reads; a read moved back onto the cached client fails the case that
// covers it rather than every case at once.

// podReadRig returns a reconciler whose cached client refuses to list Pods
// and whose APIReader holds objs, plus that store for the assertions.
func podReadRig(t *testing.T, objs ...client.Object) (*PlatformAgentReconciler, client.Client) {
	t.Helper()
	scheme := setupScheme()
	base := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...).Build()
	cached := interceptor.NewClient(base, interceptor.Funcs{
		List: func(ctx context.Context, c client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
			if _, ok := list.(*corev1.PodList); ok {
				return fmt.Errorf("Pods were listed through the manager's cache: that starts a cluster-wide Pod informer this operator never unsubscribes from, whatever namespace or labels the call narrows to. Read them through podReader")
			}
			return c.List(ctx, list, opts...)
		},
	})
	return &PlatformAgentReconciler{Client: cached, APIReader: base, Scheme: scheme}, base
}

func podReadAgent() *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}
}

// gatewayPodWaiting is a gateway pod whose container is stuck on reason.
func gatewayPodWaiting(agent *agentv1alpha1.PlatformAgent, container, reason string) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-gateway-abc123",
			Namespace: agent.Namespace,
			UID:       types.UID("gateway-pod-uid"),
			Labels:    gatewayPodSelector(agent),
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodRunning,
			ContainerStatuses: []corev1.ContainerStatus{{
				Name:  container,
				State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: reason}},
			}},
		},
	}
}

// The phase scan: up to five selectors per reconcile, every one of them a Pod
// list, on every agent on the install.
func TestTheStatusPodScanReadsPodsLive(t *testing.T) {
	agent := podReadAgent()
	pod := gatewayPodWaiting(agent, platformAgentContainerName, "ImagePullBackOff")
	r, _ := podReadRig(t, agent, pod)

	phase, reason, message := r.getDeploymentStatusDetails(context.Background(), agent, true)
	if phase != "Degraded" || reason != "ImagePullBackOff" {
		t.Errorf("phase/reason = %q/%q, want Degraded/ImagePullBackOff; the scan found no pod (message %q)", phase, reason, message)
	}
}

// A status scan that could not read the pods must not clear the fault the CR
// carries. updateStatusReady's not-Ready arm defaults to exactly this
// function's default and writes it whenever the scan does not override, so a
// "Provisioning" answer from a blind pass is written over a stored
// Degraded/ImagePullBackOff and re-enqueues the CR through an unfiltered
// watch: the fault appears to clear for a pass, then comes back. Returning
// what the CR already holds makes the computed status equal the stored one,
// and the caller's change check then writes nothing at all.
// It holds only what it wrote itself. Degraded with Ready=False is also what
// Reconcile's refusals write — a missing sandbox Secret, an unrecognised
// mode — and those clear on evidence an earlier step of the same pass has
// already gathered, so re-asserting one because a Pod list got a 429 would
// contradict a check that ran, and on a spec edit the generation witness
// forces that contradiction out to the CR.
func TestAStatusScanThatReadNoPodsHoldsTheFaultTheCRCarries(t *testing.T) {
	cases := []struct {
		name       string
		reason     string
		message    string
		wantPhase  string
		wantReason string
	}{
		{
			name:       "a fault this scan wrote is held",
			reason:     "ImagePullBackOff",
			message:    "Container 'platform-agent' in pod test-agent-gateway-abc123 is waiting: ImagePullBackOff - back-off pulling image",
			wantPhase:  "Degraded",
			wantReason: "ImagePullBackOff",
		},
		{
			name:       "an unschedulable pod this scan wrote is held",
			reason:     "PodUnschedulable",
			message:    "Pod test-agent-gateway-abc123 cannot be scheduled onto any available node: insufficient cpu.",
			wantPhase:  "Degraded",
			wantReason: "PodUnschedulable",
		},
		{
			name:       "a refusal another writer put there is not",
			reason:     reasonShellSandboxKeysMissing,
			message:    shellSandboxKeysMissingMessage("platform-agent-shell-sandbox-keys"),
			wantPhase:  "Provisioning",
			wantReason: "Provisioning",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			agent := podReadAgent()
			agent.Status.Phase = "Degraded"
			agent.Status.Conditions = []metav1.Condition{{
				Type:               "Ready",
				Status:             metav1.ConditionFalse,
				Reason:             tc.reason,
				Message:            tc.message,
				LastTransitionTime: metav1.Now(),
			}}
			scheme := setupScheme()
			refusing := interceptor.NewClient(
				fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).Build(),
				interceptor.Funcs{
					List: func(ctx context.Context, c client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
						if _, ok := list.(*corev1.PodList); ok {
							return fmt.Errorf("the API server refused the list")
						}
						return c.List(ctx, list, opts...)
					},
				})
			r := &PlatformAgentReconciler{Client: refusing, APIReader: refusing, Scheme: scheme}

			phase, reason, message := r.getDeploymentStatusDetails(context.Background(), agent, true)
			if phase != tc.wantPhase || reason != tc.wantReason {
				t.Errorf("a pass that read no pod reports %q/%q, want %q/%q (message %q)", phase, reason, tc.wantPhase, tc.wantReason, message)
			}
			if tc.wantPhase == "Degraded" && message != tc.message {
				t.Errorf("the held message is %q, want the stored %q", message, tc.message)
			}
			if tc.wantPhase != "Degraded" && message == tc.message {
				t.Errorf("the scan re-asserted another writer's message: %q", message)
			}
		})
	}
}

// The plugin scan: the same gateway pod, read a second time on every
// reconcile of an agent that has plugins.
func TestThePluginFailureScanReadsPodsLive(t *testing.T) {
	agent := podReadAgent()
	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "demo", Namespace: agent.Namespace},
		Spec:       agentv1alpha1.AgentPluginSpec{Image: "example.com/demo:v1"},
	}
	pod := gatewayPodWaiting(agent, platformAgentContainerName, reasonPodInitializing)
	pod.Status.InitContainerStatuses = []corev1.ContainerStatus{{
		Name:  buildPluginStagingContainerName(plugin.Name),
		State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: "ImagePullBackOff"}},
	}}
	r, _ := podReadRig(t, agent, pod)

	failures, err := r.detectPluginFailures(context.Background(), agent, []*agentv1alpha1.AgentPlugin{plugin})
	if err != nil {
		t.Fatalf("the plugin scan could not read the gateway's pods: %v", err)
	}
	if got, ok := failures[plugin.Name]; !ok || got.reason != pluginFailureReasonImagePull {
		t.Errorf("failures[%q] = %+v (present %v), want reason %q; the scan found no pod", plugin.Name, got, ok, pluginFailureReasonImagePull)
	}
}

// A read that failed is not an install with no plugin failures: every status
// updatePluginStatuses writes is derived from that map, so an empty one on a
// refused read writes Ready/Applied over a plugin that is not, and the patch
// re-enqueues the agent through the AgentPlugin watch to undo it next pass.
// Both halves are pinned here, because the error reaching the caller is only
// useful if the caller acts on it.
func TestAPluginStatusIsNotWrittenFromAPodReadThatFailed(t *testing.T) {
	agent := podReadAgent()
	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "demo", Namespace: agent.Namespace},
		Spec:       agentv1alpha1.AgentPluginSpec{Image: "example.com/demo:v1"},
		Status: agentv1alpha1.AgentPluginStatus{
			Phase: "Degraded",
			Conditions: []metav1.Condition{{
				Type:               "Ready",
				Status:             metav1.ConditionFalse,
				Reason:             pluginFailureReasonImagePull,
				Message:            "Plugin image pull failed",
				LastTransitionTime: metav1.Now(),
			}},
		},
	}
	scheme := setupScheme()
	refusing := interceptor.NewClient(
		fake.NewClientBuilder().
			WithScheme(scheme).
			WithObjects(agent, plugin).
			WithStatusSubresource(&agentv1alpha1.AgentPlugin{}).
			Build(),
		interceptor.Funcs{
			List: func(ctx context.Context, c client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
				if _, ok := list.(*corev1.PodList); ok {
					return fmt.Errorf("the API server refused the list")
				}
				return c.List(ctx, list, opts...)
			},
		})
	r := &PlatformAgentReconciler{Client: refusing, APIReader: refusing, Scheme: scheme}
	ctx := context.Background()

	failures, err := r.detectPluginFailures(ctx, agent, []*agentv1alpha1.AgentPlugin{plugin})
	if err == nil {
		t.Fatalf("a refused Pod list returned no error, so the caller reads %v as 'every plugin is applied'", failures)
	}

	r.updatePluginStatuses(ctx, agent, []*agentv1alpha1.AgentPlugin{plugin}, true)

	live := &agentv1alpha1.AgentPlugin{}
	if err := refusing.Get(ctx, client.ObjectKeyFromObject(plugin), live); err != nil {
		t.Fatalf("reading the AgentPlugin back: %v", err)
	}
	if live.Status.Phase != "Degraded" {
		t.Errorf("the plugin is %q after a pass that could not read the pods, want %q left standing", live.Status.Phase, "Degraded")
	}
	if cond := meta.FindStatusCondition(live.Status.Conditions, "Ready"); cond == nil || cond.Status != metav1.ConditionFalse {
		t.Errorf("the plugin's Ready condition is %+v, want the False it carried: a failed read wrote a readiness claim", cond)
	}
}

// The A2A provision report: once per Job, and the pod it reads carries the
// finding the Event is written from.
func TestTheA2AProvisionReportReadsPodsLive(t *testing.T) {
	agent := podReadAgent()
	completed := metav1.NewTime(time.Now().Add(-time.Hour))
	job := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{Name: "provision", Namespace: agent.Namespace, UID: types.UID("job-uid")},
		Status:     batchv1.JobStatus{Succeeded: 1, CompletionTime: &completed},
	}
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "provision-xyz789",
			Namespace: agent.Namespace,
			Labels: map[string]string{
				batchv1.JobNameLabel:       job.Name,
				batchv1.ControllerUidLabel: string(job.UID),
			},
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodSucceeded,
			ContainerStatuses: []corev1.ContainerStatus{{
				Name: a2aProvisionContainerName,
				State: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{
					ExitCode: 0,
					Message:  "{}",
				}},
			}},
		},
	}
	r, base := podReadRig(t, agent, job, pod)

	r.reportA2AProvisionFindings(context.Background(), agent, job)

	stamped := &batchv1.Job{}
	if err := base.Get(context.Background(), client.ObjectKeyFromObject(job), stamped); err != nil {
		t.Fatalf("reading the Job back: %v", err)
	}
	// Unreadable is what a report the pod read did not return is stamped as
	// past the grace, so the clean stamp is the one that needs the pod.
	if got := stamped.Annotations[a2aProvisionReportAnnotation]; got != a2aProvisionReportOutcomeClean {
		t.Errorf("the Job is stamped %q, want %q; the report read no pod", got, a2aProvisionReportOutcomeClean)
	}
}

// The usage poller: three Pod lists per agent — one per distinct selector
// behind its four target groups — every usageCountersPollInterval, for as long
// as the operator runs.
func TestTheUsageCounterPollerReadsPodsLive(t *testing.T) {
	agent := podReadAgent()
	pod := gatewayPodWaiting(agent, platformAgentContainerName, "")
	pod.Status.ContainerStatuses[0].State = corev1.ContainerState{Running: &corev1.ContainerStateRunning{}}
	r, _ := podReadRig(t, agent, pod)

	_, live, err := NewUsageCounterPoller(r).targets(context.Background(), agent)
	if err != nil {
		t.Fatalf("the poll could not list the agent's pods: %v", err)
	}
	if !live[string(pod.UID)] {
		t.Errorf("the gateway pod is not live after the poll (live = %v); the poll read no pod", live)
	}
}
