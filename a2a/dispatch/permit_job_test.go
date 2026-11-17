package dispatch

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"slices"
	"strings"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
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
		{perTier, "read", "kube-agents-dispatch-job-tier-tse-read"},
		{custom, "batch", "kube-agents-dispatch-job-profile-tse"},
		{custom, "read", "kube-agents-dispatch-job-tier-tse-read"},
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
// top-level task with one id never share a Job; Delete also removes the name
// a dispatcher from before the kind was in it gave the Job.
func TestAJobNameCoversTheKindAndDeleteFindsTheOldName(t *testing.T) {
	if JobName("inv", "unattended", lib.SubmitKindTop, "t") == JobName("inv", "unattended", lib.SubmitKindChild, "t") {
		t.Error("a child and a top-level task with one id share a Job name")
	}
	cfg := kubeJobsConfig(t)
	old := &batchv1.Job{ObjectMeta: metav1.ObjectMeta{Name: legacyJobName("inv", "unattended", "task-1"), Namespace: cfg.ProfileNamespace}}
	kube := fake.NewClientset(old)
	k := NewKubeJobs(kube, func() lib.DispatchConfig { return cfg })
	if err := k.Delete(context.Background(), "inv", "unattended", lib.SubmitKindTop, "task-1"); err != nil {
		t.Fatal(err)
	}
	if _, err := kube.BatchV1().Jobs(cfg.ProfileNamespace).Get(context.Background(), old.Name, metav1.GetOptions{}); !apierrors.IsNotFound(err) {
		t.Errorf("the Job under the old name is still there: %v", err)
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
