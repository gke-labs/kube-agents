/*
Copyright 2026 Google LLC.

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

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/client-go/tools/record"
)

// streamInfoJSON is a `stream info --json` answer in natscli's shape: the
// cap under config, the bytes held under state.
func streamInfoJSON(name string, maxBytes, held int64) string {
	return fmt.Sprintf(`{"config":{"name":%q,"max_bytes":%d,"discard":"old"},"state":{"messages":1,"bytes":%d,"first_seq":1}}`, name, maxBytes, held)
}

// runCapScript runs the provision script with the stub answering the given
// per-read files under dir/streams, and returns the exit, stderr, the report
// and the nats call log.
func runCapScript(t *testing.T, files map[string]string, editExit int) (int, string, string, string) {
	t.Helper()
	agent := a2aTestAgent()
	dir := t.TempDir()
	script, reportPath := stageProvisionScriptReportingTo(t, dir, agent)
	if err := os.WriteFile(reportPath, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	bash, err := exec.LookPath("bash")
	if err != nil {
		t.Skip("bash not on PATH")
	}
	log := stubNatsAnswering(t, dir, liveTASKS(agent, a2aTasksMaxMsgsPerSubject),
		topicsStateInfoJSON("a2a.topics.agent.platform.upgrade-readiness", a2aTopologySkewSubject, "a2a.topics.shared.blueprint", "a2a.topics.shared.probe"),
		profileSubmitsInfoJSON(a2aProfileSubmitMaxAge))
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(dir, "streams", name), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if editExit != 0 {
		if err := os.WriteFile(filepath.Join(dir, "streams", "edit-exit"), []byte(fmt.Sprint(editExit)), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	cmd := exec.Command(bash, script)
	cmd.Env = append(os.Environ(),
		"PATH="+filepath.Join(dir, "bin")+string(os.PathListSeparator)+os.Getenv("PATH"),
		"BUS_USER=test-agent-a2a-provision")
	var stderr strings.Builder
	cmd.Stderr, cmd.Stdout = &stderr, &strings.Builder{}
	exit := 0
	if err := cmd.Run(); err != nil {
		ee, ok := err.(*exec.ExitError)
		if !ok {
			t.Fatalf("running the script: %v", err)
		}
		exit = ee.ExitCode()
	}
	report, _ := os.ReadFile(reportPath)
	calls, _ := os.ReadFile(log)
	return exit, stderr.String(), string(report), string(calls)
}

func TestTheProvisionScriptReportsAStreamAboveItsCap(t *testing.T) {
	old := int64(20) << 30
	want := a2aTasksMaxBytes
	for _, tc := range []struct {
		name       string
		files      map[string]string
		wantExit   int
		wantReport string
		wantStderr string
	}{
		{"a near-empty stream above the cap is reported safe to lower",
			map[string]string{"TASKS.json": streamInfoJSON("TASKS", old, 1<<20)}, 0,
			fmt.Sprintf(`{"tasks_max_bytes":{"live":%d,"held":%d,"want":%d,"safe":1}}`, old, 1<<20, want),
			"lowering it now evicts nothing"},
		{"a stream holding half the new cap or more is reported not safe yet",
			map[string]string{"TASKS.json": streamInfoJSON("TASKS", old, want/2)}, 0,
			fmt.Sprintf(`{"tasks_max_bytes":{"live":%d,"held":%d,"want":%d,"safe":0}}`, old, want/2, want),
			"Once it holds less"},
		{"a stream with no byte limit is reported",
			map[string]string{"TASKS.json": streamInfoJSON("TASKS", -1, 0)}, 0,
			fmt.Sprintf(`{"tasks_max_bytes":{"live":-1,"held":0,"want":%d,"safe":1}}`, want), "carries max_bytes=-1"},
		{"a stream at the cap is left alone",
			map[string]string{"TASKS.json": streamInfoJSON("TASKS", want, 0)}, 0, `{}`, ""},
		{"a stream sized under the cap is left alone",
			map[string]string{"TASKS.json": streamInfoJSON("TASKS", want/4, 0)}, 0, `{}`, ""},
		{"an answer with no max_bytes fails",
			map[string]string{"TASKS.json": `{"config":{"name":"TASKS"},"state":{"bytes":0}}`}, 1, "", "could not read max_bytes"},
		{"an answer with no bytes fails",
			map[string]string{"TASKS.json": `{"config":{"name":"TASKS","max_bytes":1}}`}, 1, "", "could not read max_bytes"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// The closing max_consumers and subject-cap checks read TASKS'
			// first --json answer, so a case's TASKS file also carries those.
			files := map[string]string{}
			for k, v := range tc.files {
				files[k] = strings.Replace(v, `"config":{`, fmt.Sprintf(`"config":{"max_consumers":%d,"max_msgs_per_subject":%d,`, a2aTasksConsumerBudget(a2aTestAgent()), a2aTasksMaxMsgsPerSubject), 1)
			}
			exit, stderr, report, calls := runCapScript(t, files, 0)
			if exit != tc.wantExit {
				t.Fatalf("exit %d, want %d\nstderr:\n%s", exit, tc.wantExit, stderr)
			}
			if tc.wantStderr != "" && !strings.Contains(stderr, tc.wantStderr) {
				t.Errorf("stderr lacks %q:\n%s", tc.wantStderr, stderr)
			}
			if strings.Contains(calls, "stream edit") || strings.Contains(calls, "stream update") {
				t.Errorf("the script edited a stream:\n%s", calls)
			}
			if tc.wantExit != 0 {
				return
			}
			if report != tc.wantReport {
				t.Errorf("report %s, want %s", report, tc.wantReport)
			}
			if _, err := parseA2AProvisionReport(report); err != nil {
				t.Errorf("the script's report does not parse: %v", err)
			}
		})
	}
}

// TOPICS-JOURNAL goes through the same report under its own key and cap.
func TestTheProvisionScriptReportsTheJournalCap(t *testing.T) {
	old := int64(5) << 30
	exit, stderr, report, calls := runCapScript(t, map[string]string{"TOPICS-JOURNAL.json": streamInfoJSON("TOPICS-JOURNAL", old, 4096)}, 0)
	if exit != 0 {
		t.Fatalf("exit %d\n%s", exit, stderr)
	}
	if want := fmt.Sprintf(`{"topics_journal_max_bytes":{"live":%d,"held":4096,"want":%d,"safe":1}}`, old, a2aTopicsJournalMaxBytes); report != want {
		t.Errorf("report %s, want %s", report, want)
	}
	if !strings.Contains(stderr, fmt.Sprintf("nats stream edit TOPICS-JOURNAL --max-bytes=%d", a2aTopicsJournalMaxBytes)) || strings.Contains(calls, "stream edit") {
		t.Errorf("want the edit named, not run:\nstderr:\n%s\ncalls:\n%s", stderr, calls)
	}
}

// TestTheProvisionerNeverEditsAStream pins option B (#4476): the provision
// principal holds no stream update, delete or purge, so it cannot tighten a
// limit and evict, and its script runs no stream edit.
func TestTheProvisionerNeverEditsAStream(t *testing.T) {
	for _, subject := range provisionIdentity(a2aTestAgent(), "test-ns").publish {
		for _, verb := range []string{"STREAM.UPDATE", "STREAM.DELETE", "STREAM.PURGE"} {
			if strings.Contains(subject, verb) || subject == "$JS.API.>" || subject == "$JS.API.STREAM.>" {
				t.Errorf("the provisioner may %s: %s", verb, subject)
			}
		}
	}
	if strings.Contains(a2aProvisionScript(a2aTestAgent()), "$NATS stream edit") {
		t.Error("the provision script runs a stream edit")
	}
}

func TestStreamCapFindingsBecomeEvents(t *testing.T) {
	old := int64(20) << 30
	safe := fmt.Sprintf(`{"tasks_max_bytes":{"live":%d,"held":1024,"want":%d,"safe":1}}`, old, a2aTasksMaxBytes)
	f, err := parseA2AProvisionReport(safe)
	if err != nil || f.tasksMaxBytes == nil {
		t.Fatalf("findings %+v, err %v", f, err)
	}
	typ, reason, msg := f.tasksMaxBytes.event("job-a")
	if typ != corev1.EventTypeWarning || reason != reasonStreamCapAboveRender {
		t.Errorf("safe: %s/%s", typ, reason)
	}
	for _, want := range []string{"job-a", "TASKS", fmt.Sprint(old), "1024", "evicts nothing", fmt.Sprintf("nats stream edit TASKS --max-bytes=%d", a2aTasksMaxBytes)} {
		if !strings.Contains(msg, want) {
			t.Errorf("safe message lacks %q: %s", want, msg)
		}
	}

	// Not safe yet: the edit to run once it holds less, naming this render's
	// cap even when the pod's want differs.
	unsafe := fmt.Sprintf(`{"tasks_max_bytes":{"live":%d,"held":%d,"want":123456789,"safe":0}}`, old, a2aTasksMaxBytes)
	f, err = parseA2AProvisionReport(unsafe)
	if err != nil {
		t.Fatal(err)
	}
	_, _, msg = f.tasksMaxBytes.event("job-a")
	if !strings.Contains(msg, "once it holds less") || !strings.Contains(msg, fmt.Sprintf("nats stream edit TASKS --max-bytes=%d", a2aTasksMaxBytes)) {
		t.Errorf("unsafe message: %s", msg)
	}
	if strings.Contains(msg, "123456789") {
		t.Errorf("the remedy took the pod's want: %s", msg)
	}
}

func TestAStreamCapFindingTheScriptCannotWriteIsNotAReport(t *testing.T) {
	for _, message := range []string{
		`{"tasks_max_bytes":{}}`,
		`{"tasks_max_bytes":{"live":1,"held":0,"want":1}}`,
		`{"tasks_max_bytes":{"live":0,"held":0,"want":1,"safe":0}}`,
		`{"tasks_max_bytes":{"live":-2,"held":0,"want":1,"safe":0}}`,
		`{"tasks_max_bytes":{"live":5,"held":-1,"want":1,"safe":0}}`,
		`{"tasks_max_bytes":{"live":5,"held":0,"want":0,"safe":0}}`,
		`{"topics_journal_max_bytes":{"live":5,"held":0,"want":1,"safe":2}}`,
	} {
		if _, err := parseA2AProvisionReport(message); err == nil {
			t.Errorf("%s parsed as a report", message)
		}
	}
}

// Both byte-cap findings reach the PlatformAgent as Warning Events through
// the reconcile, and the Job is stamped reported.
func TestStreamCapFindingsReachThePlatformAgent(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	rec := record.NewFakeRecorder(8)
	r.Recorder = rec
	ctx := context.Background()
	provisionedInstall(t, ctx, cl, r, agent, fmt.Sprintf(
		`{"tasks_max_bytes":{"live":%d,"held":0,"want":%d,"safe":1},"topics_journal_max_bytes":{"live":%d,"held":%d,"want":%d,"safe":0}}`,
		int64(20)<<30, a2aTasksMaxBytes, int64(5)<<30, a2aTopicsJournalMaxBytes, a2aTopicsJournalMaxBytes))
	reconcileTwice(t, ctx, r, req)
	joined := strings.Join(drainEvents(rec), "\n")
	if n := strings.Count(joined, "Warning "+reasonStreamCapAboveRender); n != 2 {
		t.Errorf("got %d %s Events, want 2:\n%s", n, reasonStreamCapAboveRender, joined)
	}
	for _, want := range []string{"evicts nothing", "once it holds less", "TOPICS-JOURNAL --max-bytes=1073741824"} {
		if !strings.Contains(joined, want) {
			t.Errorf("Events lack %q:\n%s", want, joined)
		}
	}
	if got := reportedJob(t, ctx, cl, agent); got != a2aProvisionReportOutcomeReported {
		t.Errorf("Job stamp = %q, want %q", got, a2aProvisionReportOutcomeReported)
	}
}

// A stream with no byte limit reads "no limit" in its Event, not -1.
func TestAnUnlimitedStreamReadsNoLimitInItsEvent(t *testing.T) {
	for _, safe := range []int{0, 1} {
		f, err := parseA2AProvisionReport(fmt.Sprintf(`{"tasks_max_bytes":{"live":-1,"held":0,"want":%d,"safe":%d}}`, a2aTasksMaxBytes, safe))
		if err != nil {
			t.Fatal(err)
		}
		if _, _, msg := f.tasksMaxBytes.event("job-a"); !strings.Contains(msg, "no limit") || strings.Contains(msg, "-1") {
			t.Errorf("safe=%d: %s", safe, msg)
		}
	}
}
