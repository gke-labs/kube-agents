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

func TestTheProvisionScriptLowersAStreamCapOnlyWithRoom(t *testing.T) {
	old := int64(20) << 30
	want := a2aTasksMaxBytes
	edit := fmt.Sprintf("stream edit TASKS --max-bytes=%d --force", want)
	for _, tc := range []struct {
		name       string
		files      map[string]string
		editExit   int
		wantExit   int
		wantEdit   bool
		wantReport string
		wantStderr string
	}{
		{"a near-empty stream above the cap is lowered",
			map[string]string{"TASKS.json": streamInfoJSON("TASKS", old, 1<<20)}, 0, 0, true,
			fmt.Sprintf(`{"tasks_max_bytes":{"live":%d,"held":%d,"want":%d,"lowered":1}}`, old, 1<<20, want),
			"NOTE: lowered TASKS max_bytes"},
		{"a stream holding half the new cap or more is left and reported",
			map[string]string{"TASKS.json": streamInfoJSON("TASKS", old, want/2)}, 0, 0, false,
			fmt.Sprintf(`{"tasks_max_bytes":{"live":%d,"held":%d,"want":%d,"lowered":0}}`, old, want/2, want),
			fmt.Sprintf("nats stream edit TASKS --max-bytes=%d", want)},
		{"a stream that grew between the reads is left (the re-read decides)",
			map[string]string{"TASKS.1.json": streamInfoJSON("TASKS", old, 1<<20), "TASKS.2.json": streamInfoJSON("TASKS", old, 1<<20), "TASKS.3.json": streamInfoJSON("TASKS", old, want/2+1)}, 0, 0, false,
			fmt.Sprintf(`{"tasks_max_bytes":{"live":%d,"held":%d,"want":%d,"lowered":0}}`, old, want/2+1, want), "keeps max_bytes"},
		{"a stream with no byte limit is lowered when near empty",
			map[string]string{"TASKS.json": streamInfoJSON("TASKS", -1, 0)}, 0, 0, true,
			fmt.Sprintf(`{"tasks_max_bytes":{"live":-1,"held":0,"want":%d,"lowered":1}}`, want), "lowered TASKS"},
		{"a stream at the cap is left alone",
			map[string]string{"TASKS.json": streamInfoJSON("TASKS", want, 0)}, 0, 0, false, `{}`, ""},
		{"a stream sized under the cap is left alone",
			map[string]string{"TASKS.json": streamInfoJSON("TASKS", want/4, 0)}, 0, 0, false, `{}`, ""},
		{"a failed edit is reported, not fatal",
			map[string]string{"TASKS.json": streamInfoJSON("TASKS", old, 0)}, 1, 0, true,
			fmt.Sprintf(`{"tasks_max_bytes":{"live":%d,"held":0,"want":%d,"lowered":0}}`, old, want), "could not lower TASKS"},
		{"an answer with no max_bytes fails",
			map[string]string{"TASKS.json": `{"config":{"name":"TASKS"},"state":{"bytes":0}}`}, 0, 1, false, "", "could not read max_bytes"},
		{"an answer with no bytes fails",
			map[string]string{"TASKS.json": `{"config":{"name":"TASKS","max_bytes":1}}`}, 0, 1, false, "", "could not read max_bytes"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// The closing max_consumers and subject-cap checks read TASKS'
			// first --json answer, so a case's TASKS file also carries those.
			files := map[string]string{}
			for k, v := range tc.files {
				files[k] = strings.Replace(v, `"config":{`, fmt.Sprintf(`"config":{"max_consumers":%d,"max_msgs_per_subject":%d,`, a2aTasksConsumerBudget(a2aTestAgent()), a2aTasksMaxMsgsPerSubject), 1)
			}
			exit, stderr, report, calls := runCapScript(t, files, tc.editExit)
			if exit != tc.wantExit {
				t.Fatalf("exit %d, want %d\nstderr:\n%s", exit, tc.wantExit, stderr)
			}
			if tc.wantStderr != "" && !strings.Contains(stderr, tc.wantStderr) {
				t.Errorf("stderr lacks %q:\n%s", tc.wantStderr, stderr)
			}
			// A stream left at its first read is read once by the cap step,
			// after the closing checks' read: no re-read for a lower it
			// already knows it will not make.
			if tc.name == "a stream holding half the new cap or more is left and reported" {
				if n := strings.Count(calls, "stream info TASKS --json"); n != 2 {
					t.Errorf("TASKS read %d times with --json, want 2 (the closing checks', the cap step's)", n)
				}
			}
			if got := strings.Contains(calls, edit); got != tc.wantEdit {
				t.Errorf("edit called = %v, want %v\ncalls:\n%s", got, tc.wantEdit, calls)
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

// TOPICS-JOURNAL goes through the same step under its own key and cap.
func TestTheProvisionScriptLowersTheJournalCap(t *testing.T) {
	old := int64(5) << 30
	exit, stderr, report, calls := runCapScript(t, map[string]string{"TOPICS-JOURNAL.json": streamInfoJSON("TOPICS-JOURNAL", old, 4096)}, 0)
	if exit != 0 {
		t.Fatalf("exit %d\n%s", exit, stderr)
	}
	if want := fmt.Sprintf(`{"topics_journal_max_bytes":{"live":%d,"held":4096,"want":%d,"lowered":1}}`, old, a2aTopicsJournalMaxBytes); report != want {
		t.Errorf("report %s, want %s", report, want)
	}
	if !strings.Contains(calls, fmt.Sprintf("stream edit TOPICS-JOURNAL --max-bytes=%d --force", a2aTopicsJournalMaxBytes)) {
		t.Errorf("no journal edit:\n%s", calls)
	}
}

func TestStreamCapFindingsBecomeEvents(t *testing.T) {
	old := int64(20) << 30
	lowered := fmt.Sprintf(`{"tasks_max_bytes":{"live":%d,"held":1024,"want":%d,"lowered":1}}`, old, a2aTasksMaxBytes)
	f, err := parseA2AProvisionReport(lowered)
	if err != nil || f.tasksMaxBytes == nil {
		t.Fatalf("findings %+v, err %v", f, err)
	}
	typ, reason, msg := f.tasksMaxBytes.event("job-1")
	if typ != corev1.EventTypeNormal || reason != reasonStreamCapLowered {
		t.Errorf("lowered: %s/%s", typ, reason)
	}
	for _, want := range []string{"job-1", "TASKS", fmt.Sprint(old), fmt.Sprint(a2aTasksMaxBytes), "1024"} {
		if !strings.Contains(msg, want) {
			t.Errorf("lowered message lacks %q: %s", want, msg)
		}
	}

	// Left: a Warning with the exact edit, naming this render's cap even when
	// the pod's want differs.
	left := fmt.Sprintf(`{"tasks_max_bytes":{"live":%d,"held":%d,"want":123456789,"lowered":0}}`, old, a2aTasksMaxBytes)
	f, err = parseA2AProvisionReport(left)
	if err != nil {
		t.Fatal(err)
	}
	typ, reason, msg = f.tasksMaxBytes.event("job-1")
	if typ != corev1.EventTypeWarning || reason != reasonStreamCapNotLowered {
		t.Errorf("left: %s/%s", typ, reason)
	}
	if want := fmt.Sprintf("nats stream edit TASKS --max-bytes=%d", a2aTasksMaxBytes); !strings.Contains(msg, want) {
		t.Errorf("left message lacks %q: %s", want, msg)
	}
	if strings.Contains(msg, "123456789") {
		t.Errorf("the remedy took the pod's want: %s", msg)
	}

	// Under half and not lowered: the edit failed, said as such.
	failed := fmt.Sprintf(`{"topics_journal_max_bytes":{"live":%d,"held":0,"want":%d,"lowered":0}}`, int64(5)<<30, a2aTopicsJournalMaxBytes)
	f, err = parseA2AProvisionReport(failed)
	if err != nil || f.topicsJournalMaxBytes == nil {
		t.Fatalf("findings %+v, err %v", f, err)
	}
	if _, _, msg = f.topicsJournalMaxBytes.event("job-1"); !strings.Contains(msg, "the edit failed") || !strings.Contains(msg, "TOPICS-JOURNAL --max-bytes=1073741824") {
		t.Errorf("failed-edit message: %s", msg)
	}
}

func TestAStreamCapFindingTheScriptCannotWriteIsNotAReport(t *testing.T) {
	for _, message := range []string{
		`{"tasks_max_bytes":{}}`,
		`{"tasks_max_bytes":{"live":1,"held":0,"want":1}}`,
		`{"tasks_max_bytes":{"live":0,"held":0,"want":1,"lowered":0}}`,
		`{"tasks_max_bytes":{"live":-2,"held":0,"want":1,"lowered":0}}`,
		`{"tasks_max_bytes":{"live":5,"held":-1,"want":1,"lowered":0}}`,
		`{"tasks_max_bytes":{"live":5,"held":0,"want":0,"lowered":0}}`,
		`{"topics_journal_max_bytes":{"live":5,"held":0,"want":1,"lowered":2}}`,
	} {
		if _, err := parseA2AProvisionReport(message); err == nil {
			t.Errorf("%s parsed as a report", message)
		}
	}
}

// Both byte-cap findings reach the PlatformAgent as Events through the
// reconcile, and the Job is stamped reported.
func TestStreamCapFindingsReachThePlatformAgent(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	rec := record.NewFakeRecorder(8)
	r.Recorder = rec
	ctx := context.Background()
	provisionedInstall(t, ctx, cl, r, agent, fmt.Sprintf(
		`{"tasks_max_bytes":{"live":%d,"held":0,"want":%d,"lowered":1},"topics_journal_max_bytes":{"live":%d,"held":%d,"want":%d,"lowered":0}}`,
		int64(20)<<30, a2aTasksMaxBytes, int64(5)<<30, a2aTopicsJournalMaxBytes, a2aTopicsJournalMaxBytes))
	reconcileTwice(t, ctx, r, req)
	joined := strings.Join(drainEvents(rec), "\n")
	for _, want := range []string{"Normal " + reasonStreamCapLowered, "Warning " + reasonStreamCapNotLowered, "TOPICS-JOURNAL --max-bytes=1073741824"} {
		if !strings.Contains(joined, want) {
			t.Errorf("Events lack %q:\n%s", want, joined)
		}
	}
	if got := reportedJob(t, ctx, cl, agent); got != a2aProvisionReportOutcomeReported {
		t.Errorf("Job stamp = %q, want %q", got, a2aProvisionReportOutcomeReported)
	}
}
