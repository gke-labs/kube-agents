package controller

import (
	"context"
	"fmt"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Every Pod this operator reads is read live, through podReader. Nothing in
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

// A read that failed is not an install with no plugin failures: the statuses
// updatePluginStatuses writes are all derived from it, so a failing plugin
// would be written Ready/Applied and the patch would re-enqueue the agent to
// undo it on the next pass. The error has to reach the caller.
func TestThePluginFailureScanReportsAReadItCouldNotMake(t *testing.T) {
	agent := podReadAgent()
	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "demo", Namespace: agent.Namespace},
		Spec:       agentv1alpha1.AgentPluginSpec{Image: "example.com/demo:v1"},
	}
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

	failures, err := r.detectPluginFailures(context.Background(), agent, []*agentv1alpha1.AgentPlugin{plugin})
	if err == nil {
		t.Fatalf("a refused Pod list returned no error, so the caller reads %v as 'every plugin is applied'", failures)
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

// The usage poller: four Pod lists per agent, every usageCountersPollInterval,
// for as long as the operator runs.
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
