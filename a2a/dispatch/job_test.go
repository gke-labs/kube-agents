package dispatch

import (
	"context"
	"encoding/json"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// fakeJobs is a JobRunner whose task's Job has a Ready pod, job-<task>,
// readyAfter its launch (never when readyAfter is negative), or has failed.
type fakeJobs struct {
	mu         sync.Mutex
	readyAfter time.Duration
	failed     map[string]bool
	launchedAt map[string]time.Time
	launches   []string
	deletes    []string
}

func newFakeJobs(readyAfter time.Duration) *fakeJobs {
	return &fakeJobs{readyAfter: readyAfter, failed: map[string]bool{}, launchedAt: map[string]time.Time{}}
}

func (f *fakeJobs) Launch(_ context.Context, _, _, taskID string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.launches = append(f.launches, taskID)
	if _, ok := f.launchedAt[taskID]; !ok {
		f.launchedAt[taskID] = time.Now()
	}
	return nil
}

func (f *fakeJobs) Pod(_ context.Context, _, _, taskID string) (string, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failed[taskID] {
		return "", true, nil
	}
	at, ok := f.launchedAt[taskID]
	if !ok || f.readyAfter < 0 || time.Since(at) < f.readyAfter {
		return "", false, nil
	}
	return "job-" + taskID, false, nil
}

func (f *fakeJobs) Delete(_ context.Context, _, _, taskID string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.deletes = append(f.deletes, taskID)
	return nil
}

func (f *fakeJobs) deleted(taskID string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Contains(f.deletes, taskID)
}

func (f *fakeJobs) launchCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.launches)
}

// unattendedInstall is an install with one profile allowing only the
// unattended tier, with no warm pool.
func unattendedInstall(concurrency int, edit func(*lib.DispatchConfig)) *testInstall {
	prof := testProfile("inv", concurrency, "unattended")
	prof.JobTemplate = json.RawMessage(`{"spec":{"containers":[{"name":"worker","image":"worker"}]}}`)
	ti := &testInstall{src: NewStaticSource(testDispatchConfig(6, prof)), pool: NewStaticPool()}
	if edit != nil {
		ti.update(edit)
	}
	return ti
}

// An unattended task runs on a Job of its own: the Job is launched, its pod
// is offered the task once Ready, the task runs to its end, and the Job is
// deleted after.
func TestAnUnattendedTaskRunsOnItsOwnJob(t *testing.T) {
	b := newBus(t, "job-task-1")
	b.ensure("inv", 1, 0, "unattended")
	w := b.worker("inv", "unattended", "job-task-1", answerAccept, 500*time.Millisecond)
	jobs := newFakeJobs(300 * time.Millisecond)
	d, _ := b.runDispatcherWith(testConfig(), unattendedInstall(1, nil), &fakeVerifier{}, jobs)

	b.submit("inv", "unattended", lib.SubmitKindTop, "task-1")
	ts := b.waitTerminal("task-1", 15*time.Second)
	if ts.state != lib.StateCompleted {
		t.Fatalf("terminal %+v, want completed by the Job's worker", ts)
	}
	if w.offerCount() != 1 || !w.startedTask("task-1") {
		t.Errorf("the Job's pod had %d offers, started %v", w.offerCount(), w.startedTask("task-1"))
	}
	waitFor(t, 5*time.Second, "the finished task's Job deleted", func() bool { return jobs.deleted("task-1") })
	if n := d.Health().Running["inv"]; n != 0 {
		t.Errorf("%d tasks still running", n)
	}
}

// A Job with no Ready pod within jobStartSeconds is deleted, and its task
// fails no-worker (admitted, so failed) and is tombstoned, so a replay never
// launches it again. It counts against the breaker.
func TestAJobThatNeverStartsFailsItsTask(t *testing.T) {
	b := newBus(t)
	b.ensure("inv", 1, 0, "unattended")
	jobs := newFakeJobs(-1)
	ti := unattendedInstall(1, func(c *lib.DispatchConfig) { c.JobStartSeconds, c.BreakerThreshold = 2, 1 })
	d, _ := b.runDispatcherWith(testConfig(), ti, &fakeVerifier{}, jobs)

	b.submit("inv", "unattended", lib.SubmitKindTop, "task-1")
	ts := b.waitTerminal("task-1", 15*time.Second)
	if ts.state != lib.StateFailed || !hasReason(ts, ReasonNoWorker) {
		t.Fatalf("terminal %+v, want failed no-worker", ts)
	}
	if !jobs.deleted("task-1") {
		t.Error("the Job that never started was not deleted")
	}
	waitFor(t, 5*time.Second, "the refusal tombstone", func() bool {
		_, ok := b.tombstones()[lib.ProfileLeaseKey("inv", "unattended", lib.ProfileLeaseRefusedPod, "task-1")]
		return ok
	})
	if got := d.Health().OpenBreakers; len(got) != 1 || got[0] != "inv/unattended" {
		t.Errorf("open breakers %v; a Job that never starts counts as an expiry", got)
	}
}

// A Job that fails before its pod is Ready ends its task at once, without
// waiting out jobStartSeconds.
func TestAFailedJobFailsItsTaskAtOnce(t *testing.T) {
	b := newBus(t)
	b.ensure("inv", 1, 0, "unattended")
	jobs := newFakeJobs(-1)
	jobs.failed["task-1"] = true
	b.runDispatcherWith(testConfig(), unattendedInstall(1, nil), &fakeVerifier{}, jobs)

	start := time.Now()
	b.submit("inv", "unattended", lib.SubmitKindTop, "task-1")
	ts := b.waitTerminal("task-1", 15*time.Second)
	if ts.state != lib.StateFailed || !hasReason(ts, ReasonNoWorker) {
		t.Fatalf("terminal %+v, want failed no-worker", ts)
	}
	if took := time.Since(start); took > defaultJobStart/2 {
		t.Errorf("took %s; a failed Job should end its task at once", took)
	}
}

// Jobs count against the same caps as warm workers: at a concurrency of 1,
// a second unattended task waits until the first's Job is done.
func TestJobsCountAgainstTheCaps(t *testing.T) {
	b := newBus(t, "job-task-1", "job-task-2")
	b.ensure("inv", 2, 0, "unattended")
	w1 := b.worker("inv", "unattended", "job-task-1", answerAccept, 2*time.Second)
	w2 := b.worker("inv", "unattended", "job-task-2", answerAccept, 200*time.Millisecond)
	jobs := newFakeJobs(0)
	b.runDispatcherWith(testConfig(), unattendedInstall(1, nil), &fakeVerifier{}, jobs)

	b.submit("inv", "unattended", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "task-1 started", func() bool { return w1.startedTask("task-1") })
	b.submit("inv", "unattended", lib.SubmitKindTop, "task-2")
	time.Sleep(time.Second)
	if jobs.launchCount() != 1 || w2.startedCount() != 0 {
		t.Fatalf("%d Jobs launched and task-2 started %v while task-1 held the profile's one slot", jobs.launchCount(), w2.startedCount() != 0)
	}
	waitFor(t, 15*time.Second, "task-2 ran after task-1", func() bool { return len(b.terminalsOf("task-2")) > 0 })
}

// With no JobRunner, an unattended task goes to the warm pool, as before
// Job mode: an install without Jobs loses nothing.
func TestAnUnattendedTaskWithoutJobsUsesThePool(t *testing.T) {
	b := newBus(t, "inv-0")
	b.ensure("inv", 1, 0, "unattended")
	w := b.worker("inv", "unattended", "inv-0", answerAccept, 200*time.Millisecond)
	ti := unattendedInstall(1, nil)
	ti.pool.Set("inv", "unattended", "inv-0")
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	b.submit("inv", "unattended", lib.SubmitKindTop, "task-1")
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted || !w.startedTask("task-1") {
		t.Errorf("terminal %+v, started on the pool %v; want it run by the pool", ts, w.startedTask("task-1"))
	}
}

// A profile with no Job template (the chart didn't install the Job
// admission policy, so the operator rendered none) keeps the pool even with
// a JobRunner set.
func TestAProfileWithoutAJobTemplateUsesThePool(t *testing.T) {
	b := newBus(t, "inv-0")
	b.ensure("inv", 1, 0, "unattended")
	w := b.worker("inv", "unattended", "inv-0", answerAccept, 200*time.Millisecond)
	ti := unattendedInstall(1, func(c *lib.DispatchConfig) { c.Profiles[0].JobTemplate = nil })
	ti.pool.Set("inv", "unattended", "inv-0")
	jobs := newFakeJobs(0)
	b.runDispatcherWith(testConfig(), ti, &fakeVerifier{}, jobs)
	b.submit("inv", "unattended", lib.SubmitKindTop, "task-1")
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted || !w.startedTask("task-1") {
		t.Errorf("terminal %+v, started on the pool %v; want it run by the pool", ts, w.startedTask("task-1"))
	}
	if jobs.launchCount() != 0 {
		t.Errorf("%d Jobs launched for a profile with no Job template", jobs.launchCount())
	}
}

// runDispatcherWith is runDispatcher with Jobs.
func (b *bus) runDispatcherWith(cfg Config, ti *testInstall, v Verifier, jobs JobRunner) (*Dispatcher, func()) {
	return b.runDispatcherOpt(cfg, ti, v, func(d *Dispatcher) { d.UseJobs(jobs) })
}

// runDispatcherOpt is runDispatcherOn with opt applied before Run.
func (b *bus) runDispatcherOpt(cfg Config, ti *testInstall, v Verifier, opt func(*Dispatcher)) (*Dispatcher, func()) {
	b.t.Helper()
	d, err := New(cfg, b.connect("dispatcher"), ti.src, ti.pool, v, quietLog())
	if err != nil {
		b.t.Fatal(err)
	}
	opt(d)
	ctx, cancel := context.WithCancel(b.ctx)
	done := make(chan struct{})
	go func() {
		defer close(done)
		if err := d.Run(ctx); err != nil {
			b.t.Errorf("Run: %v", err)
		}
	}()
	var once sync.Once
	stop := func() {
		once.Do(func() {
			cancel()
			<-done
		})
	}
	b.t.Cleanup(stop)
	return d, stop
}

// A dispatcher stopping while an unattended task runs leaves its Job: the
// next instance re-adopts the task and needs it.
func TestStoppingKeepsARunningTasksJob(t *testing.T) {
	b := newBus(t, "job-task-1")
	b.ensure("inv", 1, 0, "unattended")
	w := b.worker("inv", "unattended", "job-task-1", answerAccept, time.Hour)
	jobs := newFakeJobs(0)
	_, stop := b.runDispatcherWith(testConfig(), unattendedInstall(1, nil), &fakeVerifier{}, jobs)

	b.submit("inv", "unattended", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "task-1 started", func() bool { return w.startedTask("task-1") })
	stop()
	if jobs.deleted("task-1") {
		t.Error("a stopping dispatcher deleted a running task's Job")
	}
}
