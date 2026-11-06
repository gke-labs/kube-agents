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

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"k8s.io/client-go/tools/record"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// PROFILE_SUBMITS is never edited once created, so a stream an older
// operator created can keep submissions longer than this render would. The
// provision script reads its max age back and reports it when it is longer,
// or unlimited (#4948); an unreadable answer fails, like the other checks.
func TestTheProvisionScriptReportsALongerProfileSubmitsMaxAge(t *testing.T) {
	agent := a2aTestAgent()
	week := 7 * 24 * time.Hour
	for _, tc := range []struct {
		name        string
		submitsJSON string
		wantExit    int
		wantReport  string
	}{
		{"a stream older and longer is the finding", profileSubmitsInfoJSON(week), 0,
			fmt.Sprintf(`{"submit_max_age":{"live":%d}}`, int64(week/time.Second))},
		{"a stream with no age limit is the finding", profileSubmitsInfoJSON(0), 0, `{"submit_max_age":{"live":0}}`},
		{"a stream as long as the render is clean", profileSubmitsInfoJSON(a2aProfileSubmitMaxAge), 0, `{}`},
		{"a shorter stream is clean (the render's own retention covers it)", profileSubmitsInfoJSON(time.Hour), 0, `{}`},
		{"an answer with no max_age fails", `{"config":{"name":"PROFILE_SUBMITS"}}`, 1, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			script, reportPath := stageProvisionScriptReportingTo(t, dir, agent)
			if err := os.WriteFile(reportPath, nil, 0o600); err != nil {
				t.Fatal(err)
			}
			bash, err := exec.LookPath("bash")
			if err != nil {
				t.Skip("bash not on PATH")
			}
			stubNatsAnswering(t, dir, liveTASKS(agent, a2aTasksMaxMsgsPerSubject),
				topicsStateInfoJSON("a2a.topics.agent.platform.upgrade-readiness", a2aTopologySkewSubject, "a2a.topics.shared.blueprint", "a2a.topics.shared.probe"),
				tc.submitsJSON)
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
			if exit != tc.wantExit {
				t.Fatalf("exit %d, want %d\nstderr:\n%s", exit, tc.wantExit, stderr.String())
			}
			if tc.wantExit != 0 {
				return
			}
			got, err := os.ReadFile(reportPath)
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != tc.wantReport {
				t.Errorf("report %s, want %s", got, tc.wantReport)
			}
			if findings, err := parseA2AProvisionReport(string(got)); err != nil {
				t.Errorf("the script's report does not parse: %v", err)
			} else if (findings.submitMaxAge != nil) != strings.Contains(tc.wantReport, "submit_max_age") {
				t.Errorf("parsed submit max age %v from %s", findings.submitMaxAge, got)
			}
		})
	}
}

// The reader takes only what the script writes under the key: a live max
// age longer than the render's, or 0. Anything else is not a report.
func TestASubmitMaxAgeFindingTheScriptCannotWriteIsNotAReport(t *testing.T) {
	render := int64(a2aProfileSubmitMaxAge / time.Second)
	for _, message := range []string{
		`{"submit_max_age":{}}`,
		`{"submit_max_age":{"live":-1}}`,
		fmt.Sprintf(`{"submit_max_age":{"live":%d}}`, render),
		`{"submit_max_age":{"live":60}}`,
	} {
		if _, err := parseA2AProvisionReport(message); err == nil {
			t.Errorf("%s parsed as a report", message)
		}
	}
	for _, message := range []string{`{"submit_max_age":{"live":0}}`, fmt.Sprintf(`{"submit_max_age":{"live":%d}}`, render+1)} {
		if f, err := parseA2AProvisionReport(message); err != nil || f.submitMaxAge == nil {
			t.Errorf("%s: findings %+v, err %v; want the max age", message, f, err)
		}
	}
}

// The dispatch config's submit retention is the longer of the render's and
// the live stream's, capped at a year; with no report yet it is the render's.
func TestTheDispatchSubmitRetentionIsTheLongerOfTheRenderAndTheLiveStream(t *testing.T) {
	render := int(a2aProfileSubmitMaxAge / time.Second)
	year := int(a2aMaxSubmitRetention / time.Second)
	week := int64(7 * 24 * time.Hour / time.Second)
	for name, tc := range map[string]struct {
		live *int64
		want int
	}{
		"no report yet":                 {nil, render},
		"a stream older and longer":     {&week, int(week)},
		"a stream with no age limit":    {ptrTo(int64(0)), year},
		"a stream longer than the cap":  {ptrTo(int64(10 * year)), year},
		"a shorter value (not written)": {ptrTo(int64(60)), render},
	} {
		agent := a2aTestAgent()
		agent.Status.ProfileSubmitMaxAgeSeconds = tc.live
		if got := dispatchSubmitRetentionSeconds(agent); got != tc.want {
			t.Errorf("%s: retention %ds, want %ds", name, got, tc.want)
		}
		cfg, err := desiredDispatchConfig(agent, nil)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if cfg.SubmitRetentionSeconds != tc.want {
			t.Errorf("%s: the rendered dispatch config carries %ds, want %ds", name, cfg.SubmitRetentionSeconds, tc.want)
		}
	}
}

// What the report says is kept on the status: a longer stream's max age is
// recorded, a later clean report clears it, and a report the operator can't
// read leaves the last value.
func TestTheProvisionReportKeepsTheLiveSubmitMaxAgeOnTheStatus(t *testing.T) {
	week := int64(7 * 24 * time.Hour / time.Second)
	for _, tc := range []struct {
		name    string
		before  *int64
		message string
		want    *int64
	}{
		{"a longer stream is recorded", nil, fmt.Sprintf(`{"submit_max_age":{"live":%d}}`, week), &week},
		{"a clean report clears it", &week, `{}`, nil},
		{"an unreadable report leaves it", &week, "", &week},
		{"no report and nothing before stays absent", nil, `{}`, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := a2aTestAgent()
			r, cl, req := a2aGateTestReconciler(t, agent)
			r.Recorder = record.NewFakeRecorder(8)
			ctx := context.Background()
			if tc.before != nil {
				live := &agentv1alpha1.PlatformAgent{}
				if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), live); err != nil {
					t.Fatal(err)
				}
				live.Status.ProfileSubmitMaxAgeSeconds = tc.before
				if err := cl.Status().Update(ctx, live); err != nil {
					t.Fatal(err)
				}
			}
			provisionedInstall(t, ctx, cl, r, agent, tc.message)
			reconcileTwice(t, ctx, r, req)
			got := &agentv1alpha1.PlatformAgent{}
			if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), got); err != nil {
				t.Fatal(err)
			}
			have := got.Status.ProfileSubmitMaxAgeSeconds
			if (have == nil) != (tc.want == nil) || (have != nil && *have != *tc.want) {
				t.Errorf("status.profileSubmitMaxAgeSeconds = %v, want %v", deref(have), deref(tc.want))
			}
		})
	}
}

func ptrTo[T any](v T) *T { return &v }

func deref(p *int64) any {
	if p == nil {
		return nil
	}
	return *p
}
