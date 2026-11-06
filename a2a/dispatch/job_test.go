package dispatch

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
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
	// pod, if set, is the name Pod reports for every Job.
	pod string
	// launchErr, if set, is what Launch returns.
	launchErr error
	// podErr, if set, is what Pod returns.
	podErr error
}

func newFakeJobs(readyAfter time.Duration) *fakeJobs {
	return &fakeJobs{readyAfter: readyAfter, failed: map[string]bool{}, launchedAt: map[string]time.Time{}}
}

func (f *fakeJobs) Launch(_ context.Context, _, _, taskID string) (time.Time, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.launches = append(f.launches, taskID)
	if f.launchErr != nil {
		return time.Time{}, f.launchErr
	}
	if _, ok := f.launchedAt[taskID]; !ok {
		f.launchedAt[taskID] = time.Now()
	}
	return f.launchedAt[taskID], nil
}

func (f *fakeJobs) Pod(_ context.Context, _, _, taskID string) (string, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.podErr != nil {
		return "", false, f.podErr
	}
	if f.failed[taskID] {
		return "", true, nil
	}
	at, ok := f.launchedAt[taskID]
	if !ok || f.readyAfter < 0 || time.Since(at) < f.readyAfter {
		return "", false, nil
	}
	if f.pod != "" {
		return f.pod, false, nil
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

// A Job-mode no-worker task is tombstoned before its failed terminal goes
// out, as a refusal is: no worker held it, so there is no lease record for
// a redelivery to adopt, and a crash between the two must not run a task
// already reported failed.
func TestAJobNoWorkerIsTombstonedBeforeItsTerminal(t *testing.T) {
	b := newBus(t)
	b.ensure("inv", 1, 0, "unattended")
	key := lib.ProfileLeaseKey("inv", "unattended", lib.ProfileLeaseRefusedPod, "task-1")
	var seen, tombstoned atomic.Bool
	b.onTerminal = func(taskID string) {
		if taskID == "task-1" && !seen.Swap(true) {
			_, ok := b.tombstones()[key]
			tombstoned.Store(ok)
		}
	}
	jobs := newFakeJobs(-1)
	jobs.failed["task-1"] = true
	b.runDispatcherWith(testConfig(), unattendedInstall(1, nil), &fakeVerifier{}, jobs)

	b.submit("inv", "unattended", lib.SubmitKindTop, "task-1")
	if ts := b.waitTerminal("task-1", 15*time.Second); ts.state != lib.StateFailed || !hasReason(ts, ReasonNoWorker) {
		t.Fatalf("terminal %+v, want failed no-worker", ts)
	}
	waitFor(t, 5*time.Second, "the terminal hook", seen.Load)
	if !tombstoned.Load() {
		t.Error("the failed no-worker terminal went out before the tombstone")
	}
}

// The start window counts from the Job's creation: a Job a restart (or an
// earlier pass) left, created longer ago than jobStartSeconds and still
// with no ready pod, fails its task at once rather than waiting afresh.
func TestAJobsStartWindowCountsFromItsCreation(t *testing.T) {
	b := newBus(t)
	b.ensure("inv", 1, 0, "unattended")
	jobs := newFakeJobs(-1)
	jobs.launchedAt["task-1"] = time.Now().Add(-time.Hour)
	b.runDispatcherWith(testConfig(), unattendedInstall(1, func(c *lib.DispatchConfig) { c.JobStartSeconds = 60 }), &fakeVerifier{}, jobs)

	start := time.Now()
	b.submit("inv", "unattended", lib.SubmitKindTop, "task-1")
	if ts := b.waitTerminal("task-1", 15*time.Second); ts.state != lib.StateFailed || !hasReason(ts, ReasonNoWorker) {
		t.Fatalf("terminal %+v, want failed no-worker", ts)
	}
	if took := time.Since(start); took > 20*time.Second {
		t.Errorf("took %s; the Job was past its start window when found", took)
	}
}

// A task whose stop is already heard when its Job would launch launches
// nothing.
func TestAStoppedTaskLaunchesNoJob(t *testing.T) {
	d := bareDispatcher()
	jobs := newFakeJobs(0)
	d.jobs = jobs
	h := &holding{profile: "inv", tier: "unattended", kind: lib.SubmitKindTop, taskID: "task-1", unattended: true, stopRequested: true}
	_, _, ended, err := d.runJob(context.Background(), quietLog(), &envDelivery{env: &lib.Envelope{TaskID: "task-1"}},
		testDispatchConfig(6, testProfile("inv", 1, "unattended")), testProfile("inv", 1, "unattended"), h)
	if !errors.Is(err, errStopped) || ended {
		t.Errorf("runJob = ended %v, %v; want errStopped", ended, err)
	}
	if n := jobs.launchCount(); n != 0 {
		t.Errorf("%d Jobs launched for a task already stopped", n)
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

// With no JobRunner, an unattended task goes to its tier's pool, when there
// is one (the operator builds pools for the read tier only).
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
// admission policy, so the operator rendered none) goes to its tier's pool,
// when there is one, even with a JobRunner set.
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
	// A pool task has no Job to delete: deleting one anyway is a refused
	// call on an install without the Jobs Role.
	time.Sleep(300 * time.Millisecond)
	jobs.mu.Lock()
	deletes := len(jobs.deletes)
	jobs.mu.Unlock()
	if deletes != 0 {
		t.Errorf("%d Job deletes for a task run on the pool", deletes)
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

// An unattended task with nothing to run it on (no Job mode for its profile,
// and no pod in its tier's pool, which the operator builds for the read tier
// only) is refused no-worker at admission, rather than left queued until its
// queue timeout (an hour here): with no JobRunner, and with one but no Job
// template.
func TestAnUnattendedTaskWithNoRunnerIsRefusedAtOnce(t *testing.T) {
	for name, withJobs := range map[string]bool{"no JobRunner": false, "no Job template": true} {
		t.Run(name, func(t *testing.T) {
			b := newBus(t)
			b.ensure("inv", 1, 0, "unattended")
			ti := unattendedInstall(1, func(c *lib.DispatchConfig) { c.Profiles[0].JobTemplate = nil })
			jobs := newFakeJobs(0)
			if withJobs {
				b.runDispatcherWith(testConfig(), ti, &fakeVerifier{}, jobs)
			} else {
				b.runDispatcher(testConfig(), ti, &fakeVerifier{})
			}
			b.submit("inv", "unattended", lib.SubmitKindTop, "task-1")
			ts := b.waitTerminal("task-1", 10*time.Second)
			if ts.state != lib.StateRejected || !hasReason(ts, ReasonNoWorker) || !strings.Contains(ts.text, "Job mode is off") {
				t.Errorf("terminal %+v, want the dispatcher's rejected %s naming Job mode", ts, ReasonNoWorker)
			}
			if jobs.launchCount() != 0 {
				t.Errorf("%d Jobs launched for a profile with no Job template", jobs.launchCount())
			}
		})
	}
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

// A Job launched before a restart, for a task whose submission now ends at
// admission (stopped, or refused), is deleted: never confirmed, its pod would
// otherwise wait for an offer until its deadline, outside every cap.
func TestAnUnstartedTasksJobIsDeletedWhenItEndsAtAdmission(t *testing.T) {
	for name, setup := range map[string]func(b *bus) Verifier{
		"stopped": func(b *bus) Verifier {
			withStopStream(b.t, b)
			b.stop("inv", "task-1")
			return &fakeVerifier{}
		},
		"refused": func(*bus) Verifier { return &fakeVerifier{refuse: map[string]bool{"cap-ok": true}} },
	} {
		t.Run(name, func(t *testing.T) {
			b := newBus(t, "job-task-1")
			b.ensure("inv", 1, 0, "unattended")
			jobs := newFakeJobs(0)
			if _, err := jobs.Launch(context.Background(), "inv", "unattended", "task-1"); err != nil { // before the restart
				t.Fatal(err)
			}
			v := setup(b)
			b.submit("inv", "unattended", lib.SubmitKindTop, "task-1")
			b.runDispatcherWith(testConfig(), unattendedInstall(1, nil), v, jobs)
			b.waitTerminal("task-1", 10*time.Second)
			waitFor(t, 5*time.Second, "the unstarted task's Job deleted", func() bool { return jobs.deleted("task-1") })
		})
	}
}

// An offer to the Job's pod that never reaches the bus is not the worker's
// failure: the task isn't failed, its Job isn't deleted, the breaker doesn't
// count it, and the submission stays queued for the next pass.
func TestAnUnsentJobOfferKeepsTheTaskQueued(t *testing.T) {
	b := newBus(t, "job-task-1")
	b.ensure("inv", 1, 0, "unattended")
	jobs := newFakeJobs(0)
	jobs.pod = "bad pod" // a subject the bus refuses, so the send fails
	d, _ := b.runDispatcherWith(testConfig(), unattendedInstall(1, func(c *lib.DispatchConfig) { c.BreakerThreshold = 1 }), &fakeVerifier{}, jobs)
	b.submit("inv", "unattended", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "a second pass", func() bool { return jobs.launchCount() >= 2 })
	if ts := b.terminalsOf("task-1"); len(ts) != 0 {
		t.Errorf("terminals %+v for a task whose offer was never sent", ts)
	}
	if jobs.deleted("task-1") {
		t.Error("the Job was deleted over an unsent offer")
	}
	if open := d.Health().OpenBreakers; len(open) != 0 {
		t.Errorf("open breakers %v over an unsent offer", open)
	}
}

// A Job the admission policy refuses is reported in Health (and logged as an
// error naming the upstream param-cache bug), the task stays queued, and the
// report clears once a Job is admitted.
func TestAJobRefusedByThePolicyIsReported(t *testing.T) {
	b := newBus(t, "job-task-1")
	b.ensure("inv", 1, 0, "unattended")
	b.worker("inv", "unattended", "job-task-1", answerAccept, 300*time.Millisecond)
	jobs := newFakeJobs(0)
	jobs.mu.Lock()
	jobs.launchErr = fmt.Errorf("%w: forbidden by kube-agents-a2a-dispatcher-job", ErrJobRefusedByPolicy)
	jobs.mu.Unlock()
	d, _ := b.runDispatcherWith(testConfig(), unattendedInstall(1, nil), &fakeVerifier{}, jobs)
	b.submit("inv", "unattended", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "the refusal in Health", func() bool { return d.Health().JobAdmission != "" })
	if ts := b.terminalsOf("task-1"); len(ts) != 0 {
		t.Errorf("terminals %+v: a refused Job keeps the task queued", ts)
	}
	jobs.mu.Lock()
	jobs.launchErr = nil
	jobs.mu.Unlock()
	if ts := b.waitTerminal("task-1", 15*time.Second); ts.state != lib.StateCompleted {
		t.Fatalf("terminal %+v once the policy admits, want completed", ts)
	}
	if h := d.Health(); h.JobAdmission != "" {
		t.Errorf("Health still reports %q after a Job was admitted", h.JobAdmission)
	}
}

// A Job that can't be read through its start limit (the API erroring, not
// the Job seen failing) is no worker's fault: the task stays queued, its Job
// isn't deleted, the breaker doesn't count it, and it shows as waiting.
func TestAnUnreadableJobAtItsStartLimitKeepsTheTaskQueued(t *testing.T) {
	b := newBus(t, "job-task-1")
	b.ensure("inv", 1, 0, "unattended")
	jobs := newFakeJobs(0)
	jobs.podErr = errors.New("the API server timed out")
	d, _ := b.runDispatcherWith(testConfig(), unattendedInstall(1, func(c *lib.DispatchConfig) {
		c.JobStartSeconds, c.BreakerThreshold = 1, 1
	}), &fakeVerifier{}, jobs)
	b.submit("inv", "unattended", lib.SubmitKindTop, "task-1")
	waitFor(t, 15*time.Second, "a second pass", func() bool { return jobs.launchCount() >= 2 })
	if ts := b.terminalsOf("task-1"); len(ts) != 0 {
		t.Errorf("terminals %+v for a task whose Job couldn't be read", ts)
	}
	if jobs.deleted("task-1") {
		t.Error("the Job was deleted though it was never seen failing")
	}
	h := d.Health()
	if len(h.OpenBreakers) != 0 {
		t.Errorf("open breakers %v over an unreadable Job", h.OpenBreakers)
	}
	waiting := 0
	for _, n := range h.Waiting {
		waiting += n
	}
	if waiting == 0 {
		t.Errorf("Health.Waiting %v: the task stays queued, so it shows as waiting", h.Waiting)
	}
}
