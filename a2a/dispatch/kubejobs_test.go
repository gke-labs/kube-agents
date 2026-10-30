package dispatch

import (
	"context"
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"testing"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apiequality "k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
	"k8s.io/utils/ptr"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The template as the operator renders it, reduced: what matters is that
// the Job carries it untouched.
func kubeJobsTemplate() corev1.PodTemplateSpec {
	return corev1.PodTemplateSpec{
		ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app.kubernetes.io/component": "profile-worker"}},
		Spec: corev1.PodSpec{
			RestartPolicy:      corev1.RestartPolicyNever,
			ServiceAccountName: "agentprofile-inv-unattended",
			Containers:         []corev1.Container{{Name: "worker", Image: "worker:1", Env: []corev1.EnvVar{{Name: "PROFILE_WORKER_ONE_SHOT", Value: "1"}}}},
		},
	}
}

func kubeJobsConfig(t *testing.T) lib.DispatchConfig {
	t.Helper()
	raw, err := json.Marshal(kubeJobsTemplate())
	if err != nil {
		t.Fatal(err)
	}
	prof := testProfile("inv", 1, "unattended")
	prof.ActiveDeadlineSeconds = 600
	prof.JobTemplate = raw
	cfg := testDispatchConfig(6, prof)
	cfg.ProfileNamespace = "ns-profiles"
	cfg.JobLabels = map[string]string{"app.kubernetes.io/instance": "ns-pa", lib.DispatchJobLabel: "true"}
	return cfg
}

// The Job is the template untouched, named by the task, with the config's
// labels and the shape the admission policy requires; a second Launch finds
// it rather than failing.
func TestTheKubeJobIsTheTemplateAsRendered(t *testing.T) {
	cfg := kubeJobsConfig(t)
	kube := fake.NewClientset()
	k := NewKubeJobs(kube, func() lib.DispatchConfig { return cfg })
	ctx := context.Background()
	for range 2 {
		if err := k.Launch(ctx, "inv", "unattended", "task-1"); err != nil {
			t.Fatal(err)
		}
	}
	job, err := kube.BatchV1().Jobs("ns-profiles").Get(ctx, JobName("inv", "unattended", "task-1"), metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if !apiequality.Semantic.DeepEqual(job.Spec.Template, kubeJobsTemplate()) {
		t.Errorf("the Job's template differs from the config's:\n got %+v\nwant %+v", job.Spec.Template, kubeJobsTemplate())
	}
	for k, v := range cfg.JobLabels {
		if job.Labels[k] != v {
			t.Errorf("Job label %s=%q, want %q", k, job.Labels[k], v)
		}
	}
	if job.Spec.BackoffLimit == nil || *job.Spec.BackoffLimit != 0 {
		t.Errorf("backoffLimit %v, want 0: a task is never re-run", job.Spec.BackoffLimit)
	}
	if job.Spec.Parallelism != nil || job.Spec.Completions != nil || job.Spec.ManualSelector != nil || job.Spec.PodFailurePolicy != nil {
		t.Errorf("the Job sets a field the policy refuses: %+v", job.Spec)
	}
	if job.Spec.TTLSecondsAfterFinished == nil {
		t.Error("no ttlSecondsAfterFinished")
	}
	if d := job.Spec.ActiveDeadlineSeconds; d == nil || *d < 600 {
		t.Errorf("activeDeadlineSeconds %v, want a backstop past the task's 600s", d)
	}
	if job.Annotations[jobTaskAnnotation] != "task-1" {
		t.Errorf("annotations %v", job.Annotations)
	}
	if JobName("inv", "unattended", "task-1") == JobName("inv", "unattended", "task-2") || len(JobName("a", "b", "c")) > 63 {
		t.Error("Job names must differ per task and fit a label value")
	}
}

// A profile the config has no template for is refused, not launched bare.
func TestAKubeJobNeedsATemplate(t *testing.T) {
	cfg := kubeJobsConfig(t)
	cfg.Profiles[0].JobTemplate = nil
	kube := fake.NewClientset()
	if err := NewKubeJobs(kube, func() lib.DispatchConfig { return cfg }).Launch(context.Background(), "inv", "unattended", "task-1"); err == nil {
		t.Fatal("launched a Job for a profile with no template")
	}
	if jobs, _ := kube.BatchV1().Jobs("ns-profiles").List(context.Background(), metav1.ListOptions{}); len(jobs.Items) != 0 {
		t.Errorf("%d Jobs created", len(jobs.Items))
	}
}

// Pod is the Job's own Ready pod; a failed Job, a failed pod or a Job gone
// is failed; a pod with the Job's label but not controlled by it is not the
// Job's. Delete is idempotent.
func TestKubeJobsFindsTheJobsPod(t *testing.T) {
	cfg := kubeJobsConfig(t)
	kube := fake.NewClientset()
	k := NewKubeJobs(kube, func() lib.DispatchConfig { return cfg })
	ctx := context.Background()
	name := JobName("inv", "unattended", "task-1")
	if _, failed, err := k.Pod(ctx, "inv", "unattended", "task-1"); err != nil || !failed {
		t.Errorf("no Job: failed %v err %v, want failed", failed, err)
	}
	if err := k.Launch(ctx, "inv", "unattended", "task-1"); err != nil {
		t.Fatal(err)
	}
	job, _ := kube.BatchV1().Jobs("ns-profiles").Get(ctx, name, metav1.GetOptions{})
	job.UID = "job-uid"
	if _, err := kube.BatchV1().Jobs("ns-profiles").Update(ctx, job, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	pod := func(n string, owner *batchv1.Job, ready bool, phase corev1.PodPhase) *corev1.Pod {
		p := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: n, Namespace: "ns-profiles", Labels: map[string]string{jobNameLabel: name}},
			Status: corev1.PodStatus{Phase: phase}}
		if owner != nil {
			p.OwnerReferences = []metav1.OwnerReference{*metav1.NewControllerRef(owner, batchv1.SchemeGroupVersion.WithKind("Job"))}
		}
		if ready {
			p.Status.Conditions = []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}}
		}
		return p
	}
	if _, err := kube.CoreV1().Pods("ns-profiles").Create(ctx, pod("impostor", nil, true, corev1.PodRunning), metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	if p, failed, err := k.Pod(ctx, "inv", "unattended", "task-1"); p != "" || failed || err != nil {
		t.Errorf("only a pod the Job doesn't control: %q %v %v, want none yet", p, failed, err)
	}
	if _, err := kube.CoreV1().Pods("ns-profiles").Create(ctx, pod("job-pod", job, true, corev1.PodRunning), metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	if p, failed, err := k.Pod(ctx, "inv", "unattended", "task-1"); p != "job-pod" || failed || err != nil {
		t.Errorf("Pod = %q %v %v, want job-pod", p, failed, err)
	}
	job.Status.Conditions = []batchv1.JobCondition{{Type: batchv1.JobFailed, Status: corev1.ConditionTrue}}
	if _, err := kube.BatchV1().Jobs("ns-profiles").Update(ctx, job, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, failed, _ := k.Pod(ctx, "inv", "unattended", "task-1"); !failed {
		t.Error("a failed Job isn't failed")
	}
	for range 2 {
		if err := k.Delete(ctx, "inv", "unattended", "task-1"); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := kube.BatchV1().Jobs("ns-profiles").Get(ctx, name, metav1.GetOptions{}); !apierrors.IsNotFound(err) {
		t.Errorf("the Job survived Delete: %v", err)
	}
}

// A failed pod of the Job's is failed, even before the Job says so.
func TestAFailedKubeJobPodIsFailed(t *testing.T) {
	cfg := kubeJobsConfig(t)
	kube := fake.NewClientset()
	k := NewKubeJobs(kube, func() lib.DispatchConfig { return cfg })
	ctx := context.Background()
	if err := k.Launch(ctx, "inv", "unattended", "task-1"); err != nil {
		t.Fatal(err)
	}
	name := JobName("inv", "unattended", "task-1")
	job, _ := kube.BatchV1().Jobs("ns-profiles").Get(ctx, name, metav1.GetOptions{})
	p := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "job-pod", Namespace: "ns-profiles", Labels: map[string]string{jobNameLabel: name},
		OwnerReferences: []metav1.OwnerReference{{APIVersion: "batch/v1", Kind: "Job", Name: name, UID: job.UID, Controller: ptr.To(true)}}},
		Status: corev1.PodStatus{Phase: corev1.PodFailed}}
	if _, err := kube.CoreV1().Pods("ns-profiles").Create(ctx, p, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, failed, err := k.Pod(ctx, "inv", "unattended", "task-1"); !failed || err != nil {
		t.Errorf("failed %v err %v, want failed", failed, err)
	}
}

// The operator's render (k8s-operator's TestRenderedDispatchJobFixture)
// decodes into lib's config, validates, and builds a Job: the template comes
// through whole (its ServiceAccount, restartPolicy Never, no token
// automount), and the Job carries the operator's labels, the dispatcher's
// marker among them, by which the operator's teardown finds it.
func TestTheRenderedJobConfigBuildsAJob(t *testing.T) {
	raw, err := os.ReadFile("testdata/rendered-dispatch-config-jobs.json")
	if err != nil {
		t.Fatal(err)
	}
	var cfg lib.DispatchConfig
	if err := json.Unmarshal(raw, &cfg); err != nil {
		t.Fatal(err)
	}
	if err := cfg.Validate(); err != nil {
		t.Fatalf("the rendered config doesn't validate: %v", err)
	}
	job, err := buildJob(cfg, "tse", "unattended", "task-1")
	if err != nil {
		t.Fatal(err)
	}
	spec := job.Spec.Template.Spec
	if spec.ServiceAccountName != "agentprofile-tse-unattended" || spec.RestartPolicy != corev1.RestartPolicyNever ||
		spec.AutomountServiceAccountToken == nil || *spec.AutomountServiceAccountToken || len(spec.Containers) == 0 {
		t.Errorf("the template didn't come through whole: %+v", spec)
	}
	if job.Labels[lib.DispatchJobLabel] != "true" || job.Labels["app.kubernetes.io/instance"] != "test-ns-test-agent" {
		t.Errorf("Job labels %v: want the operator's, with %s", job.Labels, lib.DispatchJobLabel)
	}
	if job.Namespace != "test-ns-profiles" {
		t.Errorf("namespace %q", job.Namespace)
	}
}

var updateDispatcherJob = flag.Bool("update-dispatcher-job", false, "rewrite the dispatcher Job fixture the operator's admission envtest admits")

// The operator's envtest for the dispatcher Job admission policy
// (k8s-operator/config/admission/a2a-dispatcher-job-policy.yaml) creates the
// Job in this file as the dispatcher and requires it admitted. Generated here
// by buildJob from the operator's rendered config, so a change to either the
// Job the dispatcher builds or the template the operator renders that the
// policy would refuse fails a test instead of every unattended task.
var dispatcherJobFixture = filepath.Join("..", "..", "k8s-operator", "internal", "controller", "testdata", "a2a-dispatcher-job.json")

func TestTheBuiltJobMatchesTheAdmissionFixture(t *testing.T) {
	raw, err := os.ReadFile("testdata/rendered-dispatch-config-jobs.json")
	if err != nil {
		t.Fatal(err)
	}
	var cfg lib.DispatchConfig
	if err := json.Unmarshal(raw, &cfg); err != nil {
		t.Fatal(err)
	}
	job, err := buildJob(cfg, "tse", "unattended", "task-1")
	if err != nil {
		t.Fatal(err)
	}
	job.TypeMeta = metav1.TypeMeta{APIVersion: "batch/v1", Kind: "Job"}
	got, err := json.MarshalIndent(job, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	got = append(got, '\n')
	if *updateDispatcherJob {
		if err := os.WriteFile(dispatcherJobFixture, got, 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(dispatcherJobFixture)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != string(want) {
		t.Fatalf("the Job buildJob makes no longer matches %s: rerun with -update-dispatcher-job, then the operator's TestTheDispatcherJobPolicy envtest, which admits it", dispatcherJobFixture)
	}
}
