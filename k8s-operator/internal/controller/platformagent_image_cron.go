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
	"slices"

	corev1 "k8s.io/api/core/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// builtinSchedulesEnvVar carries spec.schedules to the agent pod as a JSON document
// (imageCronPolicy). The entrypoint hands it to profile_scaffold.py, which holds off the
// suspended jobs in the Platform Agent's cron roster on every start. It goes away with that
// roster (Track 8), once every built-in is an AgentSchedule.
const builtinSchedulesEnvVar = "KUBE_AGENTS_BUILTIN_SCHEDULES"

// imageCronJobIDs are the job ids the agent image ships in the Platform Agent's cron roster
// (agents/platform/cron/jobs.json). An override naming one is applied in the agent pod, so the
// BuiltinSchedules condition doesn't report it as naming nothing.
// TestImageCronJobIDsMatchTheShippedRoster holds the list to the file.
var imageCronJobIDs = []string{
	"ai-security-audit",
	"chat-delivery-watch",
	"compliance-audit",
	"eod-event-watcher-daily-report",
	"feedback-prompt",
	"findings-morning-nudge",
	"fleet-consistency-drift",
	"fleet-wide-cost-analysis",
	"gce-compute-fleet-audit",
	"gcp-networking-fabric-audit",
	"github-repo-watcher",
	"kanban-block-recheck",
	"kanban-board-health",
	"kanban-workspace-gc",
	"leeway-watch",
	"obtainability-audit",
	"post-merge-check",
	"sa-role-watch",
	"security-patch-orchestrator",
	"session-retention",
	"stall-watch",
	"stockout-prevention",
	"upgrade-readiness-watch",
	"upgrade-readiness-weekly-brief",
	"upgrade-readiness-weekly-review",
	"upgrade-retrospective",
}

// imageCronJob reports a name the image's cron roster ships.
func imageCronJob(name string) bool {
	return slices.Contains(imageCronJobIDs, name)
}

// imageCronPolicy is builtinSchedulesEnvVar's document. profile_scaffold.py reads the same two
// keys (CRON_SUSPEND_ALL_KEY, CRON_SUSPEND_OVERRIDES_KEY).
type imageCronPolicy struct {
	// SuspendAll is spec.schedules.suspendAll.
	SuspendAll bool `json:"suspendAll"`
	// Overrides is the entries of spec.schedules.overrides that name a roster job, id to
	// suspend.
	Overrides map[string]bool `json:"overrides"`
}

// builtinSchedulesEnv is the agent container's builtinSchedulesEnvVar. It is rendered on every
// install, with nothing suspended when spec.schedules is unset, so the operator is always the
// name's last writer and an AgentPlugin's spec.env can't claim it (gatewayProfileEnvVar's
// reason).
func builtinSchedulesEnv(agent *agentv1alpha1.PlatformAgent) corev1.EnvVar {
	policy := imageCronPolicy{SuspendAll: builtinSuspendAll(agent), Overrides: map[string]bool{}}
	if agent.Spec.Schedules != nil {
		for _, o := range agent.Spec.Schedules.Overrides {
			// Only what the pod acts on: an override for an AgentSchedule, or a mistyped
			// name, must not change the pod template and restart the gateway.
			if imageCronJob(o.Name) {
				policy.Overrides[o.Name] = o.Suspend
			}
		}
	}
	// A bool and a map of bools, which json.Marshal cannot fail on; it writes map keys sorted,
	// so the value is stable across reconciles.
	document, _ := json.Marshal(policy)
	return corev1.EnvVar{Name: builtinSchedulesEnvVar, Value: string(document)}
}
