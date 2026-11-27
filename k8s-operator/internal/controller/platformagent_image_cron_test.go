/*
Copyright 2026 Google LLC

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
	"encoding/json"
	"os"
	"slices"
	"testing"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The operator's list of image cron job ids is the roster the image ships, so an override for
// any shipped job is accepted and one for a job the roster dropped is reported.
func TestImageCronJobIDsMatchTheShippedRoster(t *testing.T) {
	raw, err := os.ReadFile("../../../agents/platform/cron/jobs.json")
	if err != nil {
		t.Fatal(err)
	}
	var roster struct {
		Jobs []struct {
			ID string `json:"id"`
		} `json:"jobs"`
	}
	if err := json.Unmarshal(raw, &roster); err != nil {
		t.Fatal(err)
	}
	var shipped []string
	for _, job := range roster.Jobs {
		shipped = append(shipped, job.ID)
	}
	slices.Sort(shipped)
	if !slices.Equal(shipped, imageCronJobIDs) {
		t.Fatalf("imageCronJobIDs is out of step with agents/platform/cron/jobs.json:\n got %v\nwant %v (sorted)", imageCronJobIDs, shipped)
	}
}

// The agent container carries spec.schedules as one stable JSON document, and carries it with
// nothing suspended when spec.schedules is unset, so the operator always writes the name. An
// override naming no roster job is left out, so editing it doesn't restart the gateway.
func TestBuiltinSchedulesEnvCarriesTheSetting(t *testing.T) {
	if got := builtinSchedulesEnv(builtinTestAgent()).Value; got != `{"suspendAll":false,"overrides":{}}` {
		t.Fatalf("with spec.schedules unset: %s", got)
	}
	agent := builtinTestAgent(
		agentv1alpha1.AgentScheduleOverride{Name: "stall-watch", Suspend: false},
		agentv1alpha1.AgentScheduleOverride{Name: "compliance-audit", Suspend: true},
		agentv1alpha1.AgentScheduleOverride{Name: "compliance-audit-canary", Suspend: true},
	)
	agent.Spec.Schedules.SuspendAll = true
	want := `{"suspendAll":true,"overrides":{"compliance-audit":true,"stall-watch":false}}`
	if got := builtinSchedulesEnv(agent).Value; got != want {
		t.Fatalf("got %s, want %s", got, want)
	}
}

// The setting reaches the gateway container, which runs the roster merge, and not the dashboard
// sidecar, which runs the same entrypoint without it. (The hermes-bridge sidecar copies the
// gateway's env and never reads this.)
func TestBuiltinSchedulesEnvIsOnTheGatewayNotTheDashboard(t *testing.T) {
	agent := frontDoorAgent("schedules-env", 1, false)
	agent.Spec.Schedules = &agentv1alpha1.PlatformAgentSchedulesSpec{SuspendAll: true}
	deployment := buildDeployment(agent, "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
	got, found := envValue(containerNamed(t, deployment, "platform-agent"), builtinSchedulesEnvVar)
	if !found || got != `{"suspendAll":true,"overrides":{}}` {
		t.Fatalf("gateway %s = %q (found=%v)", builtinSchedulesEnvVar, got, found)
	}
	if _, found := envValue(containerNamed(t, deployment, "platform-agent-dashboard"), builtinSchedulesEnvVar); found {
		t.Fatalf("the dashboard sidecar carries %s; it runs no roster merge", builtinSchedulesEnvVar)
	}
}
