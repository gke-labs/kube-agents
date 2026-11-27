package dispatch

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"slices"
	"strings"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/taskcred"
)

// readPermitInstall is a read-only profile whose templates are per tier
// (#5646), so a permit-carrying task at the read tier has a Job runner, with
// pool pods for the rest.
func readPermitInstall(concurrency int, pods ...string) *testInstall {
	ti := oneProfile("tse", concurrency, pods...)
	ti.update(func(c *lib.DispatchConfig) {
		c.Profiles[0].JobTemplateRef = &lib.DispatchJobTemplateRef{Hash: "0123456789abcdef", PerTier: true}
		c.JobLabels = map[string]string{lib.DispatchJobLabel: "true"}
	})
	return ti
}

// A permit-carrying task at the read tier, of either kind, runs in its own
// one-pod Job when the profile's templates are per tier (#5646), never on a
// pool pod, and is minted its permit there. Its lease record says it ran on a
// Job, and the Job goes when it ends.
func TestAPermitTaskAtTheReadTierRunsOnItsOwnJob(t *testing.T) {
	for _, kind := range []string{lib.SubmitKindTop, lib.SubmitKindChild} {
		t.Run(kind, func(t *testing.T) {
			b := newBus(t, "tse-0", "job-task-1")
			b.ensure("tse", 2, 0, "read")
			pool := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
			job := b.worker("tse", "read", "job-task-1", answerAccept, time.Second)
			ti := readPermitInstall(2, "tse-0")
			ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
			// The Job's pod, known to the store with its UID as a real Job
			// pod is (the static store lists it to know its UID).
			ti.pool.Set("tse", "read", "tse-0", "job-task-1")
			ti.pool.SetUID("tse", "read", "job-task-1", "uid-1")
			jobs := newFakeJobs(0)
			kp, pub := signingKey(t)
			b.runKeyedWith(ti, kp, 0, &fakeVerifier{}, func(d *Dispatcher) { d.UseJobs(jobs) })

			b.submitChange("tse", "read", kind, "task-1", clusterParams)
			if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted {
				t.Fatalf("terminal %+v", ts)
			}
			if pool.offerCount() != 0 {
				t.Errorf("the pool pod was offered a permit-carrying task %d times", pool.offerCount())
			}
			confirms := confirmsOf(job)
			if len(confirms) != 1 {
				t.Fatalf("%d confirms on the task's Job pod, want one", len(confirms))
			}
			if got := verifyAs(t, confirms[0].ProxyToken, pub, taskcred.AudienceCredentialProxy).Permits; !slices.Equal(got, []taskcred.Permit{taskcred.PermitVCSProposalCreate}) {
				t.Errorf("the Job pod's proxy token permits = %v", got)
			}
			if jobs.launchCount() != 1 {
				t.Errorf("%d launches, want one", jobs.launchCount())
			}
			waitFor(t, 5*time.Second, "the finished task's Job deleted", func() bool { return jobs.deleted("task-1") })
		})
	}
}

// A read-tier task's lease record says it ran on a Job (#5646), so a
// dispatcher that re-adopts it after a restart deletes that Job when the task
// ends, even once the profile's templates are no longer per tier, when the
// tier alone no longer says a Job could exist.
func TestAReadoptedReadTierPermitTasksJobIsDeleted(t *testing.T) {
	b := newBus(t, "job-task-1")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "job-task-1", answerAccept, 4*time.Second)
	ti := readPermitInstall(2, "job-task-1")
	ti.pool.SetUID("tse", "read", "job-task-1", "uid-1")
	jobs := newFakeJobs(0)
	kp, _ := signingKey(t)
	_, _, stop1 := b.runKeyedWith(ti, kp, 0, &fakeVerifier{}, func(d *Dispatcher) { d.UseJobs(jobs) })
	b.submitChange("tse", "read", lib.SubmitKindTop, "task-1", clusterParams)
	waitFor(t, 10*time.Second, "task-1 started", func() bool { return w.startedTask("task-1") })
	stop1()
	if jobs.deleted("task-1") {
		t.Fatal("stopping deleted the running task's Job")
	}
	ti.update(func(c *lib.DispatchConfig) { c.Profiles[0].JobTemplateRef = nil })
	b.runKeyedWith(ti, kp, 0, &fakeVerifier{}, func(d *Dispatcher) { d.UseJobs(jobs) })
	if ts := b.waitTerminal("task-1", 20*time.Second); ts.state != lib.StateCompleted {
		t.Fatalf("terminal %+v, want the worker's completed", ts)
	}
	waitFor(t, 5*time.Second, "the re-adopted task's Job deleted", func() bool { return jobs.deleted("task-1") })
}

// A reference that isn't per tier is the unattended-mode tier's template
// alone (an operator from before #5646): a permit-carrying task at the read
// tier has no Job runner, so it is refused change-needs-job, not pooled.
func TestAPermitTaskWithAnUnattendedOnlyTemplateIsRefused(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
	ti := oneProfile("tse", 2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) {
		c.Profiles[0].JobTemplateRef = &lib.DispatchJobTemplateRef{Hash: "0123456789abcdef"}
	})
	ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
	jobs := newFakeJobs(0)
	kp, _ := signingKey(t)
	b.runKeyedWith(ti, kp, 0, &fakeVerifier{}, func(d *Dispatcher) { d.UseJobs(jobs) })

	b.submitChange("tse", "read", lib.SubmitKindTop, "task-1", clusterParams)
	ts := b.waitTerminal("task-1", 10*time.Second)
	if ts.state != lib.StateRejected || !strings.Contains(ts.text, "reason: "+ReasonChangeNeedsJob) {
		t.Fatalf("terminal %+v, want rejected with %s", ts, ReasonChangeNeedsJob)
	}
	if w.confirmCount() != 0 || jobs.launchCount() != 0 {
		t.Errorf("confirms %d, launches %d: the task ran", w.confirmCount(), jobs.launchCount())
	}
}

// A read-tier task with no permit stays on the warm pool, though its profile
// has a read-tier template: only confirmed changes pay a Job's cold start. A
// Job an earlier pass launched while the submission carried a permit (it is
// replaced under the same subject) goes before the pool pod is offered it.
func TestAReadTaskWithoutAPermitStaysOnThePool(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
	ti := readPermitInstall(2, "tse-0")
	ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
	jobs := newFakeJobs(0)
	if _, err := jobs.Launch(context.Background(), "tse", "read", lib.SubmitKindTop, "task-1"); err != nil { // an earlier pass
		t.Fatal(err)
	}
	kp, _ := signingKey(t)
	b.runKeyedWith(ti, kp, 0, &fakeVerifier{}, func(d *Dispatcher) { d.UseJobs(jobs) })

	b.submitWith("tse", "read", lib.SubmitKindTop, "task-1", "cap-ok", clusterParams)
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted {
		t.Fatalf("terminal %+v", ts)
	}
	if w.confirmCount() != 1 {
		t.Errorf("%d confirms on the pool pod, want one", w.confirmCount())
	}
	if n := jobs.launchCount(); n != 1 {
		t.Errorf("%d launches, want only the earlier pass's", n)
	}
	if !jobs.deleted("task-1") {
		t.Error("the earlier pass's Job was left for its pod to wait out its deadline")
	}
}

// A Job's pod is the task's own: one already holding another task is never
// offered a second, whose credentials would bind to a pod running someone
// else's work.
func TestAJobPodAlreadyHeldIsNotOfferedASecondTask(t *testing.T) {
	b := newBus(t, "shared")
	b.ensure("inv", 2, 0, "unattended")
	w := b.worker("inv", "unattended", "shared", answerAccept, 3*time.Second)
	jobs := newFakeJobs(0)
	jobs.pod = "shared"
	b.runDispatcherWith(testConfig(), unattendedInstall(2, nil), &fakeVerifier{}, jobs)

	b.submit("inv", "unattended", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "task-1 started", func() bool { return w.startedTask("task-1") })
	b.submit("inv", "unattended", lib.SubmitKindTop, "task-2")
	ts := b.waitTerminal("task-2", 10*time.Second)
	if ts.state != lib.StateFailed || !hasReason(ts, ReasonNoWorker) {
		t.Fatalf("task-2 terminal %+v, want failed %s", ts, ReasonNoWorker)
	}
	if w.startedTask("task-2") {
		t.Error("the held pod was confirmed a second task")
	}
	b.waitTerminal("task-1", 10*time.Second)
}

// The operator's read-tier param (its fixture, #5646) builds the read tier's
// Job: read from the tier's own param, never the profile's unattended-mode
// one, under the read tier's ServiceAccount and labels.
func TestTheRenderedReadTierParamBuildsAReadTierJob(t *testing.T) {
	raw, err := os.ReadFile("testdata/rendered-dispatch-config-jobs.json")
	if err != nil {
		t.Fatal(err)
	}
	var cfg lib.DispatchConfig
	if err := json.Unmarshal(raw, &cfg); err != nil {
		t.Fatal(err)
	}
	if !cfg.Profiles[0].JobTemplatesPerTier() || !cfg.Profiles[0].HasJobTemplate("read") {
		t.Fatalf("the rendered config's profile %+v has no read-tier template", cfg.Profiles[0])
	}
	param, err := os.ReadFile("testdata/rendered-dispatch-job-template-read.json")
	if err != nil {
		t.Fatal(err)
	}
	// The operator names the param with its own prefix, which must be lib's.
	var meta struct {
		Metadata struct {
			Name        string            `json:"name"`
			Annotations map[string]string `json:"annotations"`
		} `json:"metadata"`
	}
	if err := json.Unmarshal(param, &meta); err != nil {
		t.Fatal(err)
	}
	if meta.Metadata.Name != lib.DispatchJobTierTemplateName("tse", "read") {
		t.Errorf("the operator names the read tier's param %s, lib derives %s", meta.Metadata.Name, lib.DispatchJobTierTemplateName("tse", "read"))
	}
	if got := meta.Metadata.Annotations[lib.DispatchJobTemplateHashAnnotation]; got != cfg.Profiles[0].JobTemplateRef.Hash {
		t.Errorf("the read tier's param carries rendering %q, the config names %q", got, cfg.Profiles[0].JobTemplateRef.Hash)
	}
	kube := fake.NewSimpleClientset()
	k := NewKubeJobs(kube, func() lib.DispatchConfig { return cfg })
	k.getTemplate = func(_ context.Context, ns, name string) ([]byte, error) {
		if ns != cfg.ProfileNamespace || name != lib.DispatchJobTierTemplateName("tse", "read") {
			t.Fatalf("read %s/%s, want the read tier's own param", ns, name)
		}
		return param, nil
	}
	if _, err := k.Launch(context.Background(), "tse", "read", lib.SubmitKindTop, "task-1"); err != nil {
		t.Fatal(err)
	}
	job, err := kube.BatchV1().Jobs(cfg.ProfileNamespace).Get(context.Background(), JobName("tse", "read", lib.SubmitKindTop, "task-1"), metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if sa := job.Spec.Template.Spec.ServiceAccountName; sa != "agentprofile-tse-read" || job.Spec.Template.Labels[lib.ProfileTierLabel] != "read" {
		t.Errorf("the read tier's Job runs as %s with labels %v", sa, job.Spec.Template.Labels)
	}
}

// Each (profile, tier) reads its own param, derived from the pair, never one
// the config names (#5646): the unattended-mode tier's is the profile's own,
// a Tier resource's included; any other tier's is the tier's, and under a
// reference that isn't per tier there is only the profile's own.
func TestEachTierReadsItsOwnParam(t *testing.T) {
	perTier := &lib.DispatchProfile{Name: "tse", Tiers: []string{"read", "unattended"}, JobTemplateRef: &lib.DispatchJobTemplateRef{Hash: "h", PerTier: true}}
	custom := &lib.DispatchProfile{Name: "tse", Tiers: []string{"read", "batch"}, UnattendedTier: "batch", JobTemplateRef: &lib.DispatchJobTemplateRef{Hash: "h", PerTier: true}}
	legacy := &lib.DispatchProfile{Name: "tse", Tiers: []string{"read", "unattended"}, JobTemplateRef: &lib.DispatchJobTemplateRef{Hash: "h"}}
	for _, c := range []struct {
		prof       *lib.DispatchProfile
		tier, want string
	}{
		{perTier, "unattended", "kube-agents-dispatch-job-profile-tse"},
		{perTier, "read", "kube-agents-dispatch-job-tier-tse.read"},
		{custom, "batch", "kube-agents-dispatch-job-profile-tse"},
		{custom, "read", "kube-agents-dispatch-job-tier-tse.read"},
		{legacy, "unattended", "kube-agents-dispatch-job-profile-tse"},
	} {
		if got := jobTemplateName(c.prof, c.tier); got != c.want {
			t.Errorf("%s at %s (per tier %v): %s, want %s", c.prof.Name, c.tier, c.prof.JobTemplatesPerTier(), got, c.want)
		}
	}
}

// A template for another tier or profile builds no Job: it runs as that
// tier's ServiceAccount, which the admission policy would admit, so the
// dispatcher holds the template's own worker labels to the task's (#5646).
func TestATemplateForAnotherTierBuildsNoJob(t *testing.T) {
	cfg := kubeJobsConfig(t)
	prof := &cfg.Profiles[0]
	for name, labels := range map[string]map[string]string{
		"another tier":    {lib.ProfileNameLabel: "inv", lib.ProfileTierLabel: "read"},
		"another profile": {lib.ProfileNameLabel: "other", lib.ProfileTierLabel: "unattended"},
		"no labels":       nil,
	} {
		tmpl := kubeJobsTemplate()
		tmpl.Labels = labels
		raw, err := json.Marshal(tmpl)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := buildJob(cfg, prof, raw, "unattended", lib.SubmitKindTop, "task-1"); !errors.Is(err, ErrJobTemplateNotCurrent) {
			t.Errorf("%s: err %v, want ErrJobTemplateNotCurrent", name, err)
		}
	}
}

// A task's Job name covers its whole key, kind included, so a child and a
// top-level task with one id never share a Job, and the key is hashed as a
// JSON array, so two keys whose fields would join to the same string don't
// share one either.
func TestAJobNameCoversTheWholeKeyUnambiguously(t *testing.T) {
	if JobName("inv", "unattended", lib.SubmitKindTop, "t") == JobName("inv", "unattended", lib.SubmitKindChild, "t") {
		t.Error("a child and a top-level task with one id share a Job name")
	}
	if JobName("a/b", "c", lib.SubmitKindTop, "t") == JobName("a", "b/c", lib.SubmitKindTop, "t") {
		t.Error("two keys that join to the same string share a Job name")
	}
}

// A key with a part that isn't a subject token names no Job: Launch, Pod and
// Delete refuse it rather than hash it. The lib client the gateway publishes
// submissions with already refuses such a subject (lib.Client.PublishSeq's
// check); this holds without relying on it.
func TestAKeyThatIsNotSubjectTokensNamesNoJob(t *testing.T) {
	cfg := kubeJobsConfig(t)
	kube := fake.NewClientset()
	k := newKubeJobsWithParam(t, kube, &cfg)
	ctx := context.Background()
	for _, id := range []string{"a/b", "Task-1", ""} {
		if _, err := k.Launch(ctx, "inv", "unattended", lib.SubmitKindTop, id); err == nil {
			t.Errorf("launched a Job for task id %q", id)
		}
		if _, _, err := k.Pod(ctx, "inv", "unattended", lib.SubmitKindTop, id); err == nil {
			t.Errorf("looked up a Job for task id %q", id)
		}
		if err := k.Delete(ctx, "inv", "unattended", lib.SubmitKindTop, id); err == nil {
			t.Errorf("deleted a Job for task id %q", id)
		}
	}
	if jobs, _ := kube.BatchV1().Jobs(cfg.ProfileNamespace).List(ctx, metav1.ListOptions{}); len(jobs.Items) != 0 {
		t.Errorf("%d Jobs created", len(jobs.Items))
	}

}

// A read-tier task that ends at admission has the Job an earlier pass
// launched for it deleted, when its profile's templates are per tier: such a
// task may have run on a Job at the read tier (#5646), so cleanup no longer
// waits for an unattended tier to try.
func TestARefusedReadTierTasksEarlierJobIsDeleted(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	ti := readPermitInstall(2, "tse-0")
	ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
	jobs := newFakeJobs(0)
	if _, err := jobs.Launch(context.Background(), "tse", "read", lib.SubmitKindTop, "task-1"); err != nil { // an earlier pass
		t.Fatal(err)
	}
	kp, _ := signingKey(t)
	b.runKeyedWith(ti, kp, 0, &fakeVerifier{refuse: map[string]bool{"cap-ok": true}}, func(d *Dispatcher) { d.UseJobs(jobs) })
	b.submitChange("tse", "read", lib.SubmitKindTop, "task-1", clusterParams)
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateRejected {
		t.Fatalf("terminal %+v, want rejected", ts)
	}
	waitFor(t, 5*time.Second, "the earlier pass's Job deleted", func() bool { return jobs.deleted("task-1") })
}

// A permit-carrying task's Job failing to start at the read tier fails that
// task, but doesn't count on the read tier's breaker: the breaker guards the
// warm pool there, so read tasks on the pool keep being served.
func TestReadTierJobFailuresDontHoldBackThePool(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, 200*time.Millisecond)
	ti := readPermitInstall(2, "tse-0")
	ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
	jobs := newFakeJobs(0)
	kp, _ := signingKey(t)
	b.runKeyedWith(ti, kp, 0, &fakeVerifier{}, func(d *Dispatcher) { d.UseJobs(jobs) })
	for i := range defaultBreakerThreshold + 1 {
		id := fmt.Sprintf("change-%d", i)
		jobs.mu.Lock()
		jobs.failed[id] = true
		jobs.mu.Unlock()
		b.submitChange("tse", "read", lib.SubmitKindTop, id, clusterParams)
		if ts := b.waitTerminal(id, 10*time.Second); ts.state != lib.StateFailed || !hasReason(ts, ReasonNoWorker) {
			t.Fatalf("%s terminal %+v, want failed %s", id, ts, ReasonNoWorker)
		}
	}
	b.submitWith("tse", "read", lib.SubmitKindTop, "plain", "cap-ok", clusterParams)
	if ts := b.waitTerminal("plain", 10*time.Second); ts.state != lib.StateCompleted {
		t.Fatalf("a pool read task after failing read-tier Jobs: terminal %+v, want completed", ts)
	}
	if !w.startedTask("plain") {
		t.Error("the pool pod never ran the read task")
	}
}

// A Job an earlier pass launched at the read tier is deleted when its task
// ends at admission even after the profile's template reference is gone:
// under Job mode cleanup is attempted whatever the reference says now.
func TestAnUnstartedReadTierJobIsDeletedAfterItsReferenceIsLost(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	ti := oneProfile("tse", 2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.JobLabels = map[string]string{lib.DispatchJobLabel: "true"} })
	ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
	jobs := newFakeJobs(0)
	if _, err := jobs.Launch(context.Background(), "tse", "read", lib.SubmitKindTop, "task-1"); err != nil { // while the profile had a read template
		t.Fatal(err)
	}
	kp, _ := signingKey(t)
	b.runKeyedWith(ti, kp, 0, &fakeVerifier{refuse: map[string]bool{"cap-ok": true}}, func(d *Dispatcher) { d.UseJobs(jobs) })
	b.submitChange("tse", "read", lib.SubmitKindTop, "task-1", clusterParams)
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateRejected {
		t.Fatalf("terminal %+v, want rejected", ts)
	}
	waitFor(t, 5*time.Second, "the earlier pass's Job deleted", func() bool { return jobs.deleted("task-1") })
}

// A Job found under the task's name is used only if it is the dispatcher's
// Job for that task, with a pod template running as the task's template
// does: an impostor someone else made under the name is refused at Launch,
// and a Job swapped in after Launch, or its pod, is refused at Pod, so no
// permit is minted for a pod the dispatcher didn't shape.
func TestAJobFoundByNameMustBeTheTasksOwn(t *testing.T) {
	cfg := kubeJobsConfig(t)
	ctx := context.Background()
	own := func(t *testing.T) *batchv1.Job {
		j, err := buildJob(cfg, &cfg.Profiles[0], kubeJobsTemplateRaw(t), "unattended", lib.SubmitKindTop, "task-1")
		if err != nil {
			t.Fatal(err)
		}
		return j
	}
	for name, mutate := range map[string]func(*batchv1.Job){
		"no dispatcher marker":      func(j *batchv1.Job) { delete(j.Labels, lib.DispatchJobLabel) },
		"another task's annotation": func(j *batchv1.Job) { j.Annotations[jobTaskAnnotation] = "task-9" },
		"another kind's annotation": func(j *batchv1.Job) { j.Annotations[jobKindAnnotation] = lib.SubmitKindChild },
		"another ServiceAccount":    func(j *batchv1.Job) { j.Spec.Template.Spec.ServiceAccountName = "agentprofile-inv-admin" },
	} {
		t.Run(name, func(t *testing.T) {
			impostor := own(t)
			mutate(impostor)
			k := newKubeJobsWithParam(t, fake.NewClientset(impostor), &cfg)
			if _, err := k.Launch(ctx, "inv", "unattended", lib.SubmitKindTop, "task-1"); !errors.Is(err, ErrJobNotOurs) {
				t.Errorf("Launch over an impostor: %v, want ErrJobNotOurs", err)
			}
			if _, _, err := k.Pod(ctx, "inv", "unattended", lib.SubmitKindTop, "task-1"); !errors.Is(err, ErrJobNotOurs) {
				t.Errorf("Pod of an impostor never checked: %v, want ErrJobNotOurs", err)
			}
		})
	}

	// A Job this dispatcher never launched or checked is refused at Pod even
	// if its template names no ServiceAccount to compare.
	bare := own(t)
	bare.Spec.Template.Spec.ServiceAccountName = ""
	kb := newKubeJobsWithParam(t, fake.NewClientset(bare), &cfg)
	if _, _, err := kb.Pod(ctx, "inv", "unattended", lib.SubmitKindTop, "task-1"); !errors.Is(err, ErrJobNotOurs) {
		t.Errorf("Pod of a never-checked Job with no ServiceAccount: %v, want ErrJobNotOurs", err)
	}

	// Launched and checked, then swapped for an impostor, or holding a pod
	// that runs as another ServiceAccount: Pod refuses both.
	kube := fake.NewClientset()
	k := newKubeJobsWithParam(t, kube, &cfg)
	if _, err := k.Launch(ctx, "inv", "unattended", lib.SubmitKindTop, "task-1"); err != nil {
		t.Fatal(err)
	}
	name := JobName("inv", "unattended", lib.SubmitKindTop, "task-1")
	job, err := kube.BatchV1().Jobs(cfg.ProfileNamespace).Get(ctx, name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "job-pod", Namespace: cfg.ProfileNamespace, Labels: map[string]string{jobNameLabel: name},
		OwnerReferences: []metav1.OwnerReference{*metav1.NewControllerRef(job, batchv1.SchemeGroupVersion.WithKind("Job"))}},
		Spec:   corev1.PodSpec{ServiceAccountName: "agentprofile-inv-admin"},
		Status: corev1.PodStatus{Phase: corev1.PodRunning, Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}}}}
	if _, err := kube.CoreV1().Pods(cfg.ProfileNamespace).Create(ctx, pod, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	if p, _, err := k.Pod(ctx, "inv", "unattended", lib.SubmitKindTop, "task-1"); p != "" || !errors.Is(err, ErrJobNotOurs) {
		t.Errorf("a pod running as another ServiceAccount: %q, %v, want refused", p, err)
	}
	// The pod is gone, so only the Job's own check can refuse what follows.
	if err := kube.CoreV1().Pods(cfg.ProfileNamespace).Delete(ctx, "job-pod", metav1.DeleteOptions{}); err != nil {
		t.Fatal(err)
	}
	job.Annotations[jobTaskAnnotation] = "task-9"
	if _, err := kube.BatchV1().Jobs(cfg.ProfileNamespace).Update(ctx, job, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, _, err := k.Pod(ctx, "inv", "unattended", lib.SubmitKindTop, "task-1"); !errors.Is(err, ErrJobNotOurs) {
		t.Errorf("a Job swapped after Launch: %v, want ErrJobNotOurs", err)
	}
}

// permitBreakerBus runs readPermitInstall with a Job runner, a pool pod and a
// Job pod for task id, and gives back the dispatcher, so a test can set the
// read tier's breaker. The pool's tier breaker guards the pool; a
// permit-carrying task's own Job at that tier (#5646) stays clear of it.
func permitBreakerBus(t *testing.T, id string, jobAnswer time.Duration, noRenew bool) (*bus, *Dispatcher, *fakeWorker) {
	t.Helper()
	b := newBus(t, "tse-0", "job-"+id)
	b.ensure("tse", 2, 0, "read")
	pool := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
	job := b.worker("tse", "read", "job-"+id, answerAccept, jobAnswer)
	job.noRenew = noRenew
	ti := readPermitInstall(2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.BreakerThreshold, c.BreakerCooldownSeconds = 1, 600 })
	ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
	ti.pool.Set("tse", "read", "tse-0", "job-"+id)
	ti.pool.SetUID("tse", "read", "job-"+id, "uid-1")
	kp, _ := signingKey(t)
	var d *Dispatcher
	b.runKeyedWith(ti, kp, 0, &fakeVerifier{}, func(dd *Dispatcher) { d = dd; d.UseJobs(newFakeJobs(0)) })
	return b, d, pool
}

func readBreaker(d *Dispatcher) (br breaker, ok bool) {
	d.breakers.mu.Lock()
	defer d.breakers.mu.Unlock()
	p, ok := d.breakers.m[ptKey{"tse", "read"}]
	if ok {
		br = *p
	}
	return br, ok
}

// An open read breaker doesn't hold back a permit-carrying task's own Job,
// and that task's finish doesn't close the pool's breaker: a pool task still
// waits.
func TestAPermitJobRunsPastAnOpenPoolBreakerAndLeavesItOpen(t *testing.T) {
	b, d, pool := permitBreakerBus(t, "task-1", time.Second, false)
	d.breakers.expired(d.src.Config(), "tse", "read", time.Now())
	if got := d.Health().OpenBreakers; len(got) != 1 {
		t.Fatalf("open breakers %v, want the read tier's", got)
	}
	b.submitChange("tse", "read", lib.SubmitKindTop, "task-1", clusterParams)
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted {
		t.Fatalf("permit task under an open pool breaker: terminal %+v, want completed", ts)
	}
	if got := d.Health().OpenBreakers; len(got) != 1 || got[0] != "tse/read" {
		t.Errorf("open breakers after the permit task = %v, want tse/read still open", got)
	}
	b.submitWith("tse", "read", lib.SubmitKindTop, "plain", "cap-ok", clusterParams)
	time.Sleep(2 * time.Second) // several retries
	if ts := b.terminalsOf("plain"); len(ts) != 0 || pool.startedTask("plain") {
		t.Errorf("a pool task ran under the open breaker: terminals %+v", ts)
	}
}

// A permit-carrying task's own Job is never the pool's half-open probe: its
// finish leaves the half-open breaker for a pool task to close.
func TestAPermitJobIsNotTheHalfOpenPoolBreakersProbe(t *testing.T) {
	b, d, _ := permitBreakerBus(t, "task-1", time.Second, false)
	d.breakers.expired(d.src.Config(), "tse", "read", time.Now())
	d.breakers.mu.Lock()
	d.breakers.m[ptKey{"tse", "read"}].openUntil = time.Now().Add(-time.Second) // past the cooldown: half open
	d.breakers.mu.Unlock()
	b.submitChange("tse", "read", lib.SubmitKindTop, "task-1", clusterParams)
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted {
		t.Fatalf("terminal %+v", ts)
	}
	if br, ok := readBreaker(d); !ok || !br.halfOpen {
		t.Errorf("the read breaker after a permit Job's finish = %+v (present %v), want still half open", br, ok)
	}
}

// A permit-carrying task's Job whose worker stops renewing fails the task
// lease-expired, but isn't counted on the pool's breaker.
func TestAPermitJobsLostWorkerDoesNotOpenThePoolBreaker(t *testing.T) {
	b, d, _ := permitBreakerBus(t, "task-1", time.Hour, true)
	b.submitChange("tse", "read", lib.SubmitKindTop, "task-1", clusterParams)
	if ts := b.waitTerminal("task-1", 20*time.Second); !hasReason(ts, ReasonLeaseExpired) {
		t.Fatalf("terminal %+v, want lease-expired", ts)
	}
	if br, ok := readBreaker(d); ok {
		t.Errorf("the read breaker after a permit Job's lost worker = %+v, want none", br)
	}
}

// A permit-carrying task's Job re-adopted after a restart still stays clear
// of the pool's breaker: its lost worker fails it lease-expired without
// opening the read tier's breaker.
func TestAReadoptedPermitJobsLostWorkerDoesNotOpenThePoolBreaker(t *testing.T) {
	b := newBus(t, "job-task-1")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "job-task-1", answerAccept, time.Hour)
	ti := readPermitInstall(2, "job-task-1")
	ti.update(func(c *lib.DispatchConfig) { c.BreakerThreshold, c.BreakerCooldownSeconds = 1, 600 })
	ti.pool.SetUID("tse", "read", "job-task-1", "uid-1")
	jobs := newFakeJobs(0)
	kp, _ := signingKey(t)
	_, _, stop1 := b.runKeyedWith(ti, kp, 0, &fakeVerifier{}, func(d *Dispatcher) { d.UseJobs(jobs) })
	b.submitChange("tse", "read", lib.SubmitKindTop, "task-1", clusterParams)
	waitFor(t, 10*time.Second, "task-1 started", func() bool { return w.startedTask("task-1") })
	stop1()
	w.killed.Store(true)
	var d *Dispatcher
	b.runKeyedWith(ti, kp, 0, &fakeVerifier{}, func(dd *Dispatcher) { d = dd; d.UseJobs(jobs) })
	if ts := b.waitTerminal("task-1", 30*time.Second); !hasReason(ts, ReasonLeaseExpired) {
		t.Fatalf("terminal %+v, want lease-expired", ts)
	}
	if br, ok := readBreaker(d); ok {
		t.Errorf("the read breaker after a re-adopted permit Job's lost worker = %+v, want none", br)
	}
}
