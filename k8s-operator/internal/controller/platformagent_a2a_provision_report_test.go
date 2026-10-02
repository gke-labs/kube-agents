/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

// The per-subject-cap gap, from the Job's pod to an Event on the CR.
//
// The provision script's closing block finds a TASKS stream with no
// per-subject cap and, being create-only, cannot close it: it prints a NOTE
// and exits 0. Before this the NOTE was the whole record, on a pod log the
// Job's TTL removes within a day, and the CR read Ready throughout (#1735).
// Now the script also writes the finding to its container's termination
// message, and the reconcile that first sees the Job complete reads it off
// the pod and records a Warning Event on the PlatformAgent, once per Job
// run. These tests drive both ends: the script against a stubbed nats, and
// Reconcile against a fake recorder.

import (
	"context"
	"fmt"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/uuid"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// stageProvisionScriptReportingTo stages the script the way the consumer
// budget tests do, with one more substitution: the termination message path,
// which only exists inside a pod, points at a file under dir. The
// substitution is asserted, as the token path's is, so a script that stops
// writing there fails here rather than passing on a file nothing wrote. The
// file is not created: a case that wants the kubelet's mount present creates
// it, and a case that wants it absent leaves it.
func stageProvisionScriptReportingTo(t *testing.T, dir string, agent *agentv1alpha1.PlatformAgent) (script, reportPath string) {
	t.Helper()
	reportPath = filepath.Join(dir, "termination-log")
	raw := a2aProvisionScript(agent)
	// The guard and the redirect: two sites, both the same constant.
	if n := strings.Count(raw, a2aProvisionTerminationLogPath); n != 2 {
		t.Fatalf("expected the provision script to name %q twice (the -w guard and the write), found %d; this test would otherwise run a script it did not finish adapting",
			a2aProvisionTerminationLogPath, n)
	}
	return stageProvisionScript(t, dir, strings.ReplaceAll(raw, a2aProvisionTerminationLogPath, reportPath)), reportPath
}

// runProvisionScript runs a staged script against a stubbed nats answering
// `stream info TASKS --json` with liveJSON, and returns the exit status and
// stderr.
func runProvisionScript(t *testing.T, dir, script, liveJSON string) (int, string) {
	t.Helper()
	bash, err := exec.LookPath("bash")
	if err != nil {
		t.Skip("bash not on PATH")
	}
	stubNats(t, dir, liveJSON)
	cmd := exec.Command(bash, script)
	cmd.Env = append(os.Environ(),
		"PATH="+filepath.Join(dir, "bin")+string(os.PathListSeparator)+os.Getenv("PATH"),
		"BUS_USER=test-agent-a2a-provision",
	)
	var stderr strings.Builder
	cmd.Stderr = &stderr
	cmd.Stdout = &strings.Builder{}
	runErr := cmd.Run()
	if runErr == nil {
		return 0, stderr.String()
	}
	ee, ok := runErr.(*exec.ExitError)
	if !ok {
		t.Fatalf("running the provision script: %v\nstderr:\n%s", runErr, stderr.String())
	}
	return ee.ExitCode(), stderr.String()
}

// liveTASKS is a stream info answer whose consumer budget fits this agent,
// so the closing block's max_consumers refusal stays out of the way and the
// per-subject cap is the only thing under test.
func liveTASKS(agent *agentv1alpha1.PlatformAgent, subjectCap int) string {
	return fmt.Sprintf(`{"name":"TASKS","max_consumers":%d,"max_msgs_per_subject":%d}`, a2aTasksConsumerBudget(agent), subjectCap)
}

// TestTheProvisionScriptReportsTheSubjectCapGapOnItsTerminationMessage: the
// unbounded stream writes the finding, with the live and rendered numbers;
// a bounded stream, at the rendered cap or another, writes the empty
// report; and the exit status is 0 throughout, because the gap is a report
// and not a failure.
func TestTheProvisionScriptReportsTheSubjectCapGapOnItsTerminationMessage(t *testing.T) {
	agent := a2aTestAgent()
	want := a2aTasksMaxMsgsPerSubject
	for _, tc := range []struct {
		name       string
		liveCap    int
		wantReport string
		wantStderr string
	}{
		{
			name:       "no cap at all is the gap",
			liveCap:    0,
			wantReport: fmt.Sprintf(`{"tasks_subject_cap":{"live":0,"want":%d}}`, want),
			wantStderr: "no per-subject limit",
		},
		{
			name:       "the unlimited sentinel is the gap too",
			liveCap:    -1,
			wantReport: fmt.Sprintf(`{"tasks_subject_cap":{"live":-1,"want":%d}}`, want),
			wantStderr: "no per-subject limit",
		},
		{
			name:       "the rendered cap is clean",
			liveCap:    want,
			wantReport: `{}`,
		},
		{
			name:       "another finite cap is drift, not the gap",
			liveCap:    8,
			wantReport: `{}`,
			wantStderr: "this is drift between",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			script, reportPath := stageProvisionScriptReportingTo(t, dir, agent)
			// The kubelet's mount, present.
			if err := os.WriteFile(reportPath, nil, 0o600); err != nil {
				t.Fatal(err)
			}
			exit, stderr := runProvisionScript(t, dir, script, liveTASKS(agent, tc.liveCap))
			if exit != 0 {
				t.Fatalf("exit %d, want 0: the gap is a report, not a failure\nstderr:\n%s", exit, stderr)
			}
			if tc.wantStderr != "" && !strings.Contains(stderr, tc.wantStderr) {
				t.Errorf("stderr lost the human-readable NOTE %q; the termination message is a copy, not a replacement:\n%s", tc.wantStderr, stderr)
			}
			got, err := os.ReadFile(reportPath)
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != tc.wantReport {
				t.Errorf("termination message = %q, want %q", got, tc.wantReport)
			}
			// And the two ends agree: what the script wrote is what the
			// reconcile parses.
			finding, err := parseA2AProvisionReport(string(got))
			if err != nil {
				t.Fatalf("the reconcile cannot read what the script wrote: %v", err)
			}
			if tc.liveCap == 0 || tc.liveCap == -1 {
				if finding == nil || finding.live != int64(tc.liveCap) || !finding.hasWant || finding.want != int64(want) {
					t.Errorf("parsed finding = %+v, want live %d want %d", finding, tc.liveCap, want)
				}
			} else if finding != nil {
				t.Errorf("a bounded stream parsed as a finding: %+v", finding)
			}
		})
	}
}

// TestTheProvisionScriptDoesNotFailWithoutATerminationLog: outside a pod the
// kubelet's file is not there, and a copy of a report is not a reason to
// fail a fully provisioned bus.
func TestTheProvisionScriptDoesNotFailWithoutATerminationLog(t *testing.T) {
	agent := a2aTestAgent()
	dir := t.TempDir()
	script, reportPath := stageProvisionScriptReportingTo(t, dir, agent)
	exit, stderr := runProvisionScript(t, dir, script, liveTASKS(agent, 0))
	if exit != 0 {
		t.Fatalf("exit %d, want 0\nstderr:\n%s", exit, stderr)
	}
	if _, err := os.Stat(reportPath); !os.IsNotExist(err) {
		t.Errorf("the script created the termination log itself (stat err %v); it may only write where the kubelet mounted one", err)
	}
	if !strings.Contains(stderr, "no per-subject limit") {
		t.Errorf("the NOTE still has to reach the pod log when there is no termination log:\n%s", stderr)
	}
}

// TestTheProvisionScriptSurvivesAFailedTerminationLogWrite: the kubelet's
// file is there and writable by the -w test, and the write itself fails (a
// full or faulting disk; here, a directory in the file's place, which every
// platform refuses to write a stream into). Under set -e a bare failed write
// would exit 1 on a bus the script just finished provisioning, and exit 1
// matches no podFailurePolicy rule, so the Job would burn its backoff into
// BackoffLimitExceeded and the CR would read Degraded on a complete bus. The
// script has to say so on stderr and exit 0; the reconcile then sees no
// message and stamps the Job unreadable rather than clean.
func TestTheProvisionScriptSurvivesAFailedTerminationLogWrite(t *testing.T) {
	agent := a2aTestAgent()
	dir := t.TempDir()
	script, reportPath := stageProvisionScriptReportingTo(t, dir, agent)
	if err := os.Mkdir(reportPath, 0o700); err != nil {
		t.Fatal(err)
	}
	exit, stderr := runProvisionScript(t, dir, script, liveTASKS(agent, 0))
	if exit != 0 {
		t.Fatalf("exit %d, want 0: a failed report write is not a failed provision\nstderr:\n%s", exit, stderr)
	}
	if !strings.Contains(stderr, "could not write the provision report to the termination log") {
		t.Errorf("the failed write goes unmentioned on stderr:\n%s", stderr)
	}
	if !strings.Contains(stderr, "no per-subject limit") {
		t.Errorf("the NOTE still has to reach the pod log when the termination log cannot be written:\n%s", stderr)
	}
}

// assigningUIDsOnCreate gives every object the reconciler creates a UID, the
// way the API server does and the fake client (controller-runtime v0.25) does
// not. The report reader selects the Job's pods by controller-uid as well as
// job-name; with the fake's empty UID on the Job and an empty label on the
// pods, that half of the selector matched everything and pinned nothing.
func assigningUIDsOnCreate(funcs interceptor.Funcs) interceptor.Funcs {
	inner := funcs.Create
	funcs.Create = func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
		if obj.GetUID() == "" {
			obj.SetUID(uuid.NewUUID())
		}
		if inner != nil {
			return inner(ctx, cl, obj, opts...)
		}
		return cl.Create(ctx, obj, opts...)
	}
	return funcs
}

// provisionJobOf reads the provision Job back as stored, so pods built for
// it carry the labels the Job controller would give them: the name and the
// Job's UID, which is what the reader selects on. A Job with no UID would
// make the controller-uid half of that selector vacuous, so it is refused
// here rather than matched by accident.
func provisionJobOf(t *testing.T, ctx context.Context, cl client.Client, agent *agentv1alpha1.PlatformAgent) *batchv1.Job {
	t.Helper()
	job := &batchv1.Job{}
	if err := cl.Get(ctx, types.NamespacedName{Name: buildA2AProvisionJob(agent).Name, Namespace: agent.Namespace}, job); err != nil {
		t.Fatalf("get provision Job: %v", err)
	}
	if job.UID == "" {
		t.Fatal("precondition: the provision Job has no UID, so the reader's controller-uid selector would match any pod under the name; build the client with assigningUIDsOnCreate")
	}
	return job
}

func jobPodLabels(job *batchv1.Job) map[string]string {
	return map[string]string{batchv1.JobNameLabel: job.Name, batchv1.ControllerUidLabel: string(job.UID)}
}

// provisionPodReporting is the pod that completed the provision Job: its
// container terminated with exit 0 and message on its termination message.
func provisionPodReporting(job *batchv1.Job, suffix, message string) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      job.Name + "-" + suffix,
			Namespace: job.Namespace,
			Labels:    jobPodLabels(job),
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodSucceeded,
			ContainerStatuses: []corev1.ContainerStatus{{
				Name:  a2aProvisionContainerName,
				State: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{ExitCode: 0, Message: message}},
			}},
		},
	}
}

// provisionPodRetried is an earlier pod of the same Job that failed before
// NATS answered: exit 1, no message. The Job controller leaves it behind, and
// the reader has to skip it.
func provisionPodRetried(job *batchv1.Job) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      job.Name + "-retry",
			Namespace: job.Namespace,
			Labels:    jobPodLabels(job),
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodFailed,
			ContainerStatuses: []corev1.ContainerStatus{{
				Name:  a2aProvisionContainerName,
				State: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{ExitCode: 1}},
			}},
		},
	}
}

// provisionPodOfAnotherJob is a succeeded pod under the Job's name that a
// different Job owns: the previous Job of the same digested name, deleted by
// hand and re-created, whose pods the garbage collector has not reached.
// It carries message, and the reader must not read it.
func provisionPodOfAnotherJob(job *batchv1.Job, message string) *corev1.Pod {
	pod := provisionPodReporting(job, "previous-run", message)
	pod.Labels[batchv1.ControllerUidLabel] = "some-other-uid"
	return pod
}

// drainEvents returns every Event the fake recorder has buffered, without
// waiting for more.
func drainEvents(rec *record.FakeRecorder) []string {
	var out []string
	for {
		select {
		case e := <-rec.Events:
			out = append(out, e)
		default:
			return out
		}
	}
}

// reportedJob reads the provision Job back and returns its report stamp.
func reportedJob(t *testing.T, ctx context.Context, cl client.Client, agent *agentv1alpha1.PlatformAgent) string {
	t.Helper()
	job := &batchv1.Job{}
	if err := cl.Get(ctx, types.NamespacedName{Name: buildA2AProvisionJob(agent).Name, Namespace: agent.Namespace}, job); err != nil {
		t.Fatalf("get provision Job: %v", err)
	}
	return job.Annotations[a2aProvisionReportAnnotation]
}

// provisionedInstall renders the stack, completes the Job and leaves the
// pods the Job controller would have left: a failed retry and the succeeded
// run carrying message.
func provisionedInstall(t *testing.T, ctx context.Context, cl client.Client, r *PlatformAgentReconciler, agent *agentv1alpha1.PlatformAgent, message string) string {
	t.Helper()
	theCalloutIsServing(t, ctx, cl, r, agent)
	completeTheProvisionJob(t, ctx, cl, agent)
	job := provisionJobOf(t, ctx, cl, agent)
	for _, pod := range []*corev1.Pod{provisionPodRetried(job), provisionPodReporting(job, "run1", message)} {
		if err := cl.Create(ctx, pod); err != nil {
			t.Fatal(err)
		}
	}
	return job.Name
}

// reconcileTwice is the two passes a fresh CR needs to reach the A2A render:
// the first adds the finalizer and returns, as every Reconcile test in this
// package allows for.
func reconcileTwice(t *testing.T, ctx context.Context, r *PlatformAgentReconciler, req ctrl.Request) {
	t.Helper()
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}
}

func gapReport() string {
	return fmt.Sprintf(`{"tasks_subject_cap":{"live":0,"want":%d}}`, a2aTasksMaxMsgsPerSubject)
}

// TestTheSubjectCapGapIsAnEventOnTheCROncePerJobRun is the mechanism end to
// end through Reconcile: the finding on the pod becomes exactly one Warning
// Event naming the reason, the live and rendered caps, the Job and the
// remedy; later passes over the same completed Job add nothing; and the
// Job's next life (the TTL took it, create-if-absent ran it again) reports
// again, because the gap is still there.
func TestTheSubjectCapGapIsAnEventOnTheCROncePerJobRun(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	rec := record.NewFakeRecorder(8)
	r.Recorder = rec
	ctx := context.Background()
	jobName := provisionedInstall(t, ctx, cl, r, agent, gapReport())

	reconcileTwice(t, ctx, r, req)
	events := drainEvents(rec)
	if len(events) != 1 {
		t.Fatalf("got %d Events on the passes that saw the Job complete, want exactly 1: %q", len(events), events)
	}
	for _, want := range []string{
		corev1.EventTypeWarning, reasonTasksSubjectCapMissing, jobName,
		"max_msgs_per_subject=0", fmt.Sprintf("creates it at %d", a2aTasksMaxMsgsPerSubject),
		fmt.Sprintf("nats stream edit TASKS --max-msgs-per-subject=%d", a2aTasksMaxMsgsPerSubject),
	} {
		if !strings.Contains(events[0], want) {
			t.Errorf("the Event does not carry %q:\n%s", want, events[0])
		}
	}
	if got := reportedJob(t, ctx, cl, agent); got != a2aProvisionReportOutcomeReported {
		t.Errorf("Job stamp = %q, want %q", got, a2aProvisionReportOutcomeReported)
	}

	// The same completed Job, three more passes: nothing.
	for i := 0; i < 3; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+2, err)
		}
	}
	if events := drainEvents(rec); len(events) != 0 {
		t.Fatalf("a later pass over the same completed Job recorded %d more Event(s); the stamp is not holding: %q", len(events), events)
	}

	// The TTL: the Job and its pods go; create-if-absent runs it again under
	// the same name; it finds the same gap.
	job := &batchv1.Job{ObjectMeta: metav1.ObjectMeta{Name: jobName, Namespace: agent.Namespace}}
	if err := cl.Delete(ctx, job); err != nil {
		t.Fatal(err)
	}
	pods := &corev1.PodList{}
	if err := cl.List(ctx, pods, client.InNamespace(agent.Namespace), client.MatchingLabels{batchv1.JobNameLabel: jobName}); err != nil {
		t.Fatal(err)
	}
	for i := range pods.Items {
		if err := cl.Delete(ctx, &pods.Items[i]); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile after the TTL: %v", err)
	}
	if got := reportedJob(t, ctx, cl, agent); got != "" {
		t.Fatalf("the re-created Job already carries stamp %q; a fresh run must start unstamped", got)
	}
	completeTheProvisionJob(t, ctx, cl, agent)
	if err := cl.Create(ctx, provisionPodReporting(provisionJobOf(t, ctx, cl, agent), "run2", gapReport())); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile on the re-run: %v", err)
	}
	if events := drainEvents(rec); len(events) != 1 {
		t.Fatalf("got %d Events on the Job's second run, want exactly 1 (the gap is still there): %q", len(events), events)
	}
}

// TestACleanProvisionReportIsNoEvent: the script found the cap in place and
// wrote {}; the Job is stamped so the pod is not re-read, and nothing is
// recorded.
func TestACleanProvisionReportIsNoEvent(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	rec := record.NewFakeRecorder(8)
	r.Recorder = rec
	ctx := context.Background()
	provisionedInstall(t, ctx, cl, r, agent, `{}`)
	reconcileTwice(t, ctx, r, req)
	if events := drainEvents(rec); len(events) != 0 {
		t.Fatalf("a clean report recorded %d Event(s): %q", len(events), events)
	}
	if got := reportedJob(t, ctx, cl, agent); got != a2aProvisionReportOutcomeClean {
		t.Errorf("Job stamp = %q, want %q", got, a2aProvisionReportOutcomeClean)
	}
}

// TestAnEmptyTerminationMessageIsNotClean: no message at all is the script
// never having written, not a clean bill. It is stamped so the pod is not
// re-read every pass, but as unreadable, and no Event is invented.
func TestAnEmptyTerminationMessageIsNotClean(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	rec := record.NewFakeRecorder(8)
	r.Recorder = rec
	ctx := context.Background()
	provisionedInstall(t, ctx, cl, r, agent, "")
	reconcileTwice(t, ctx, r, req)
	if events := drainEvents(rec); len(events) != 0 {
		t.Fatalf("an empty message recorded %d Event(s): %q", len(events), events)
	}
	if got := reportedJob(t, ctx, cl, agent); got != a2aProvisionReportOutcomeUnreadable {
		t.Errorf("Job stamp = %q, want %q", got, a2aProvisionReportOutcomeUnreadable)
	}
}

// TestTheReportWaitsForThePodAndReportsOnce: the Job reads complete before
// its pod is in the cache. That pass stamps nothing, so the next one, with
// the pod there, still reports; and it reports exactly once.
func TestTheReportWaitsForThePodAndReportsOnce(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	rec := record.NewFakeRecorder(8)
	r.Recorder = rec
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	completeTheProvisionJob(t, ctx, cl, agent)
	reconcileTwice(t, ctx, r, req)
	if events := drainEvents(rec); len(events) != 0 {
		t.Fatalf("an Event with no pod to read: %q", events)
	}
	if got := reportedJob(t, ctx, cl, agent); got != "" {
		t.Fatalf("the Job was stamped %q with no pod read; the report would be lost", got)
	}
	if err := cl.Create(ctx, provisionPodReporting(provisionJobOf(t, ctx, cl, agent), "late", gapReport())); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d with the pod: %v", i+1, err)
		}
	}
	if events := drainEvents(rec); len(events) != 1 {
		t.Fatalf("got %d Events once the pod was there, want 1: %q", len(events), events)
	}
}

// TestAPodOfAnotherJobUnderTheSameNameIsNotRead pins the controller-uid half
// of the reader's selector. A Job deleted by hand and re-created under the
// same digested name shares the namespace with the previous Job's pods until
// the garbage collector reaches them; one of those, succeeded and carrying a
// gap report, is not this Job's run. With no pod of its own in the cache the
// Job takes the waiting path while its completion is fresh (no Event, no
// stamp) and the vanished-pod path once it is older than the grace (no
// Event, stamped unreadable); the other Job's report reaches the
// PlatformAgent's Events on neither.
func TestAPodOfAnotherJobUnderTheSameNameIsNotRead(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	rec := record.NewFakeRecorder(8)
	r.Recorder = rec
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	completeTheProvisionJob(t, ctx, cl, agent)
	job := provisionJobOf(t, ctx, cl, agent)
	if err := cl.Create(ctx, provisionPodOfAnotherJob(job, gapReport())); err != nil {
		t.Fatal(err)
	}

	reconcileTwice(t, ctx, r, req)
	if events := drainEvents(rec); len(events) != 0 {
		t.Fatalf("another Job's pod was read as this Job's run: %d Event(s): %q", len(events), events)
	}
	if got := reportedJob(t, ctx, cl, agent); got != "" {
		t.Fatalf("Job stamp = %q on a fresh completion with none of its own pods in the cache; want no stamp, the pass waits", got)
	}

	ageTheProvisionJob(t, ctx, cl, agent, 2*a2aProvisionReportGrace)
	reconcileTwice(t, ctx, r, req)
	if events := drainEvents(rec); len(events) != 0 {
		t.Fatalf("another Job's pod was read once the Job's own pod counted as gone: %d Event(s): %q", len(events), events)
	}
	if got := reportedJob(t, ctx, cl, agent); got != a2aProvisionReportOutcomeUnreadable {
		t.Errorf("Job stamp = %q, want %q: the Job's own pod is gone and the other Job's is not a substitute", got, a2aProvisionReportOutcomeUnreadable)
	}
}

// TestAReconcilerWithoutARecorderStillStampsTheJob: tests and the golden
// harness build the reconciler with no recorder. The pass must not panic,
// and the stamp still lands so the pod is not re-read forever.
func TestAReconcilerWithoutARecorderStillStampsTheJob(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	if r.Recorder != nil {
		t.Fatal("precondition: the harness set a recorder")
	}
	ctx := context.Background()
	provisionedInstall(t, ctx, cl, r, agent, gapReport())
	reconcileTwice(t, ctx, r, req)
	if got := reportedJob(t, ctx, cl, agent); got != a2aProvisionReportOutcomeReported {
		t.Errorf("Job stamp = %q, want %q", got, a2aProvisionReportOutcomeReported)
	}
}

// ageTheProvisionJob moves the provision Job's completion back by age, so a
// pass reads it as an old completion rather than one the cache may still be
// catching up with.
func ageTheProvisionJob(t *testing.T, ctx context.Context, cl client.Client, agent *agentv1alpha1.PlatformAgent, age time.Duration) {
	t.Helper()
	job := provisionJobOf(t, ctx, cl, agent)
	completed := metav1.NewTime(time.Now().Add(-age))
	job.Status.CompletionTime = &completed
	if err := cl.Status().Update(ctx, job); err != nil {
		t.Fatalf("age provision Job: %v", err)
	}
}

// a2aGateTestReconcilerCountingProvisionPodLists is a2aGateTestReconciler
// with one more interceptor: it counts every List of Pods selected by the
// Job-name label, which is the read reportA2AProvisionFindings makes and
// nothing else in Reconcile does.
func a2aGateTestReconcilerCountingProvisionPodLists(t *testing.T, agent *agentv1alpha1.PlatformAgent) (*PlatformAgentReconciler, client.Client, ctrl.Request, *int) {
	t.Helper()
	lists := 0
	funcs := assigningUIDsOnCreate(fakeServerSideApplyInterceptors())
	funcs.List = func(ctx context.Context, cl client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
		if _, isPods := list.(*corev1.PodList); isPods {
			lo := &client.ListOptions{}
			lo.ApplyOptions(opts)
			if lo.LabelSelector != nil && strings.Contains(lo.LabelSelector.String(), batchv1.JobNameLabel) {
				lists++
			}
		}
		return cl.List(ctx, list, opts...)
	}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, sandboxKeysSecret(agent), discordBotSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(funcs).
		Build()
	return &PlatformAgentReconciler{Client: cl, Scheme: scheme}, cl,
		ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}, &lists
}

// TestAJobWhoseSucceededPodIsGoneIsStampedUnreadableOnce: the Job completed
// and its succeeded pod is no longer there to read (the pod garbage
// collector after a node scale-down, a Succeeded-pod cleanup, a hand
// delete). That is not "the cache has not caught up": the Job counted the
// success and the completion is well past the grace. The run's report is
// lost, and the pass says so once, stamping the Job unreadable, rather than
// re-listing the pods and re-logging on every pass for the rest of the
// Job's 24h TTL. No Event is invented for it.
func TestAJobWhoseSucceededPodIsGoneIsStampedUnreadableOnce(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req, lists := a2aGateTestReconcilerCountingProvisionPodLists(t, agent)
	rec := record.NewFakeRecorder(8)
	r.Recorder = rec
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	completeTheProvisionJob(t, ctx, cl, agent)
	ageTheProvisionJob(t, ctx, cl, agent, 2*a2aProvisionReportGrace)
	*lists = 0

	reconcileTwice(t, ctx, r, req)
	if events := drainEvents(rec); len(events) != 0 {
		t.Fatalf("an Event with no pod to read: %q", events)
	}
	if got := reportedJob(t, ctx, cl, agent); got != a2aProvisionReportOutcomeUnreadable {
		t.Fatalf("Job stamp = %q, want %q: a Job whose pod is gone has to be stamped, or it is re-read for its whole TTL", got, a2aProvisionReportOutcomeUnreadable)
	}
	if *lists != 1 {
		t.Fatalf("the Job's pods were listed %d times across the passes that saw it complete, want exactly 1", *lists)
	}
	// Three more passes over the same stamped Job: no list, no stamp, no Event.
	for i := 0; i < 3; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+3, err)
		}
	}
	if *lists != 1 {
		t.Fatalf("a stamped Job's pods were listed again (%d lists in all); the stamp is not holding", *lists)
	}
	if events := drainEvents(rec); len(events) != 0 {
		t.Fatalf("a later pass recorded %d Event(s): %q", len(events), events)
	}
}

// TestAFreshCompletionWithNoPodStillWaits is the other side of the grace:
// the Job completed just now, its counted pod is not in the cache yet, and
// the pass leaves it unstamped for the next one. TestTheReportWaitsForThe-
// PodAndReportsOnce covers the report that then arrives; this pins that the
// wait is decided by the completion's age and not by the count alone, so
// the vanished-pod branch cannot swallow a report the cache was about to
// deliver.
func TestAFreshCompletionWithNoPodStillWaits(t *testing.T) {
	job := &batchv1.Job{}
	job.Status.Succeeded = 1
	now := time.Now()
	fresh := metav1.NewTime(now.Add(-a2aProvisionReportGrace / 2))
	job.Status.CompletionTime = &fresh
	if a2aProvisionPodVanished(job, now) {
		t.Fatal("a completion half a grace old read as a vanished pod")
	}
	old := metav1.NewTime(now.Add(-a2aProvisionReportGrace))
	job.Status.CompletionTime = &old
	if !a2aProvisionPodVanished(job, now) {
		t.Fatal("a completion a whole grace old still read as the cache catching up")
	}
	job.Status.CompletionTime = nil
	if !a2aProvisionPodVanished(job, now) {
		t.Fatal("a counted success with no completion time has nothing to bound the wait on and must read as gone")
	}
	job.Status.Succeeded = 0
	if a2aProvisionPodVanished(job, now) {
		t.Fatal("a Job that counted no success has no pod to have lost")
	}
}

// TestAFindingWithoutALiveCapIsMalformed: the reader does not fill in a
// field the pod left out, and does not quote one the script could not have
// written. A tasks_subject_cap finding with no live cap, or with a live cap
// other than the two spellings of no limit the script writes under that key
// (0 and -1), is not a report: the Job is stamped unreadable, and no Event
// is recorded, because the only Event the reader could build from it would
// carry numbers the pod did not send. A message that is JSON null is no
// report either: the script's clean report is {}, and null decodes to no
// object at all rather than to an object with nothing in it.
func TestAFindingWithoutALiveCapIsMalformed(t *testing.T) {
	for _, tc := range []struct{ name, message string }{
		{"no fields at all", `{"tasks_subject_cap":{}}`},
		{"null in the finding's place", `{"tasks_subject_cap":null}`},
		{"null in the report's place", `null`},
		{"want but no live", fmt.Sprintf(`{"tasks_subject_cap":{"want":%d}}`, a2aTasksMaxMsgsPerSubject)},
		{"a positive live cap is a bound, not the gap", fmt.Sprintf(`{"tasks_subject_cap":{"live":8,"want":%d}}`, a2aTasksMaxMsgsPerSubject)},
		{"a negative live cap other than -1 is nothing nats reports", fmt.Sprintf(`{"tasks_subject_cap":{"live":-7,"want":%d}}`, a2aTasksMaxMsgsPerSubject)},
		{"the most negative live cap the field can hold", fmt.Sprintf(`{"tasks_subject_cap":{"live":%d}}`, int64(math.MinInt64))},
		{"a live cap that is not a number", `{"tasks_subject_cap":{"live":"0"}}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := a2aTestAgent()
			r, cl, req := a2aGateTestReconciler(t, agent)
			rec := record.NewFakeRecorder(8)
			r.Recorder = rec
			ctx := context.Background()
			provisionedInstall(t, ctx, cl, r, agent, tc.message)
			reconcileTwice(t, ctx, r, req)
			if events := drainEvents(rec); len(events) != 0 {
				t.Fatalf("a malformed finding recorded %d Event(s): %q", len(events), events)
			}
			if got := reportedJob(t, ctx, cl, agent); got != a2aProvisionReportOutcomeUnreadable {
				t.Errorf("Job stamp = %q, want %q", got, a2aProvisionReportOutcomeUnreadable)
			}
		})
	}
}

// TestTheRemedyCarriesThisRendersCapNotThePods: the nats stream edit in the
// Event names this binary's a2aTasksMaxMsgsPerSubject whatever the pod's
// report said the rendered cap was. A finding with no want at all still
// gets the right remedy (and never --max-msgs-per-subject=0, which to nats
// is no limit); a finding whose want differs gets the right remedy and a
// remark naming the pod's number, so the difference is said, not trusted.
func TestTheRemedyCarriesThisRendersCapNotThePods(t *testing.T) {
	rendered := fmt.Sprintf("nats stream edit TASKS --max-msgs-per-subject=%d", a2aTasksMaxMsgsPerSubject)
	for _, tc := range []struct {
		name, message string
		wantRemark    string
	}{
		{name: "no want at all", message: `{"tasks_subject_cap":{"live":0}}`},
		{name: "the unlimited sentinel with no want", message: `{"tasks_subject_cap":{"live":-1}}`},
		{name: "a want of zero", message: `{"tasks_subject_cap":{"live":0,"want":0}}`, wantRemark: "named 0 as the rendered cap"},
		{name: "some other want", message: `{"tasks_subject_cap":{"live":0,"want":7}}`, wantRemark: "named 7 as the rendered cap"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := a2aTestAgent()
			r, cl, req := a2aGateTestReconciler(t, agent)
			rec := record.NewFakeRecorder(8)
			r.Recorder = rec
			ctx := context.Background()
			provisionedInstall(t, ctx, cl, r, agent, tc.message)
			reconcileTwice(t, ctx, r, req)
			events := drainEvents(rec)
			if len(events) != 1 {
				t.Fatalf("got %d Events, want 1: %q", len(events), events)
			}
			if strings.Contains(events[0], "--max-msgs-per-subject=0") {
				t.Fatalf("the Event tells the operator to remove the limit:\n%s", events[0])
			}
			if !strings.Contains(events[0], rendered) {
				t.Errorf("the Event's remedy is not this render's %q:\n%s", rendered, events[0])
			}
			if !strings.Contains(events[0], fmt.Sprintf("creates it at %d", a2aTasksMaxMsgsPerSubject)) {
				t.Errorf("the Event's rendered cap is not this render's:\n%s", events[0])
			}
			if tc.wantRemark == "" && strings.Contains(events[0], "as the rendered cap") {
				t.Errorf("a finding with no want drew a remark about the pod's want:\n%s", events[0])
			}
			if tc.wantRemark != "" && !strings.Contains(events[0], tc.wantRemark) {
				t.Errorf("the Event does not say the pod's want differed (%q):\n%s", tc.wantRemark, events[0])
			}
			if got := reportedJob(t, ctx, cl, agent); got != a2aProvisionReportOutcomeReported {
				t.Errorf("Job stamp = %q, want %q", got, a2aProvisionReportOutcomeReported)
			}
		})
	}
}
