# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# The deployer scans this directory's *.tf only, so every variable that has to
# reach the stack is declared here. Two rules it enforces are worth knowing
# before adding one: an INJECTED variable this stack does not declare is
# dropped silently (which is why `cluster_name`, `node_count` and
# `machine_type` are absent -- this stack builds no cluster and would only be
# confused by them), while a variable named in a task.yaml `variables:` block
# and NOT declared here raises ConfigError. So the task file and this one are a
# matched pair.

# The cluster the Platform Agent pod runs in, and the only cluster this
# scenario can use -- not because k8s-event-watcher is confined to the cluster
# it runs in (it fans in over the Cluster Agent profile clusters as well), but
# because a per-run cluster joins that watch set too late to be watched inside
# the run. main.tf's header carries the argument. hack/ci-eval-pr.sh passes
# these two from HOST_CLUSTER_NAME/REGION -- deliberately not from the per-run
# task cluster the other tofu stacks build, which has no agent on it.
variable "host_cluster_name" {
  type        = string
  description = "Name of the cluster the Platform Agent runs in; the incident is planted here."
}

variable "host_cluster_location" {
  type        = string
  description = "Region or zone of host_cluster_name."
}

variable "project_id" {
  type        = string
  description = "GCP Project ID holding host_cluster_name"
  default     = ""
}

# Where the agent install lives, so the poll below can read the watcher's own
# log and prove the pipeline started rather than infer it from a timer.
variable "agent_namespace" {
  type        = string
  description = "Namespace of the kube-agents install on host_cluster_name"
  default     = "kubeagents-system"
}

variable "agent_deployment" {
  type        = string
  description = "Deployment running the Platform Agent pod, whose logs carry the watcher's dispatch lines"
  default     = "platform-agent-gateway"
}

variable "agent_container" {
  type = string
  # k8s-event-watcher is not a container of its own: it runs inside the
  # agent-api-auth sidecar of the gateway pod (buildAgentAPIAuthSidecar in
  # k8s-operator/internal/controller/platformagent_manifests.go). It used to
  # sit beside envoy-credential-proxy, which #913 moved to its own pod on
  # 2026-09-05, and a default still naming it timed out the watcher wait on
  # every apply (#3681). Step 0a of main.tf refuses a container the pod does
  # not have, before anything is planted; tests/test_autoops_incident_plant.py
  # pins this default to the operator's agentAPIAuthContainerName.
  description = "Container inside agent_deployment that k8s-event-watcher runs inside"
  default     = "agent-api-auth"
}

# ---------------------------------------------------------------------------
# The planted incident. Every value below is also written into
# bench/tasks/autoops-warning-event-triage/task.yaml -- in its prompt, which
# names the namespace the agent looks for, and in its safeguards, which assert
# the limit and the allocation came out unchanged. Change one and change both;
# the task file's `variables:` block sits directly above the checks that repeat
# them, so the pair is visible in one screen.
# ---------------------------------------------------------------------------

# Static, not per-BUILD_ID. Two runs cannot collide on it: Boskos leases one
# project per run so no two runs share a host cluster, and no other task
# names this namespace (the fan-out can co-run tasks, so uniqueness across
# tasks is what matters now, not loop order). Nor can the watcher's 24h dedup window suppress a second
# run's incident, which is the failure a static name would usually invite --
# the dedup key is {pod UID, reason} (k8s-event-watcher/types.go, EventKey), and
# every run's Deployment mints a fresh pod with a fresh UID.
variable "incident_namespace" {
  type        = string
  description = "Namespace the incident workload is planted in, and torn down with"
  default     = "eval-autoops-incident"
}

# Carries no diagnostic content on purpose. The report_contains objective keyed
# on this name proves the triage identified the right workload; a name like
# "eval-oomkill" would also satisfy the objective that asks for the root cause,
# which would then be measuring transcription rather than diagnosis.
variable "workload_name" {
  type        = string
  description = "Name of the Deployment whose container is OOM-killed"
  default     = "eval-incident-workload"
}

# The two numbers are the whole scenario, and the gap between them is what
# gives the triage two honest remediations rather than one: raise the limit to
# fit the workload, or shrink the workload to fit the limit. Both are one-line
# edits to the manifest, which is what the report is asked to propose.
#
# A workload that allocated WITHOUT bound would have only one sound fix -- no
# limit fits it -- and the case's judge reference, which grades a report on
# surfacing both, would then be asking the agent to pad its answer. Keep the
# allocation bounded and keep it above the limit.
variable "memory_limit_mib" {
  type        = number
  description = "The container's memory limit, in MiB. Must be below allocate_mib."
  default     = 64
}

variable "allocate_mib" {
  type        = number
  description = "Single bounded allocation the container makes, in MiB. Must exceed memory_limit_mib."
  default     = 96
}

# More than one is incident-triage-workload-fold's scenario: every replica
# crash-loops the same way at once, so the watcher opens one incident per pod
# and the daemon has to fold them into one.
variable "replicas" {
  type        = number
  description = "Replicas of the OOM-killed Deployment"
  default     = 1
}

# incident-triage-workload-fold's repetitions run a few minutes apart, inside
# the daemon's fold window, so a later repetition's replicas fold into the
# first one's incident and the watcher logs `folded` rather than `fire`. That
# case grades the fold and sets this; the others wait on their own card and
# leave it off.
variable "accept_folded" {
  type        = bool
  description = "Count the watcher's `folded` log line, as well as `fire`, as the incident being handled"
  default     = false
}


# The second shape the stack can plant: a container that exits non-zero until
# crash_seconds have passed since its pod started, then runs. kubelet backs it
# off (Reason=BackOff), the back-off climbs 10s, 20s, 40s, 80s, 160s, so the
# restarts fall at about 10s, 30s, 70s, 150s and 310s, and kubelet re-emits
# the BackOff event on every sync while a back-off is pending, so the event's
# sightings span the whole time until a restart succeeds. 45 runs on the 70s
# restart, inside the watcher's 2m settle period, and must raise no card; 240
# outlasts it and must. 0 (the default) plants the OOM shape above instead.
variable "crash_seconds" {
  type        = number
  description = "Seconds the planted container keeps crashing before it starts; 0 plants the OOM-killed shape."
  default     = 0
}
# What the apply waits for before returning: `incident`, the watcher's log
# line that it opened an incident for the namespace (the OOM shape's only
# sensible wait); `followup`, the watcher's log line that it sent the quiet
# follow-up for the namespace, which arrives WATCHER_QUIET_AFTER (15m) after
# the pod's last event, and which the apply stops waiting for at its bound
# without failing, so a case can grade the follow-up's absence.
variable "wait_for" {
  type        = string
  description = "What the apply waits for: incident or followup."
  default     = "incident"
  validation {
    condition     = contains(["incident", "followup"], var.wait_for)
    error_message = "wait_for must be incident or followup."
  }
}

# Seconds to sleep after the watcher poll before handing control to the agent
# turn (same settle pattern as bench/tf/prebuilt/triage-start-burst). The
# front-door reader (`default` profile) has no `sleep` tool and is not held in
# `_await_delegated_work` when it files no cards of its own, so a fast burst of
# `kanban_list`/`kanban_show` reads can exhaust its turn while sequential cards
# (such as `INCIDENT_TRIAGE_OPEN_PULL_REQUEST=true`'s triage card followed by
# its parented Pull Request card) are still running. Defaults to 0 so
# single-card cases keep their existing timing; multi-card cases size this as a
# wall-clock head start for card execution before the board read starts.
variable "start_settle_seconds" {
  type        = number
  description = "Seconds to wait after the watcher poll before the agent turn so filed incident cards have a head start toward completion before the board read starts."
  default     = 0

  validation {
    condition     = var.start_settle_seconds >= 0 && var.start_settle_seconds <= 86400 && floor(var.start_settle_seconds) == var.start_settle_seconds
    error_message = "start_settle_seconds must be a whole number of seconds between 0 and 86400."
  }
}

# Identify the CI run that planted this, so a janitor can find what a run killed
# before teardown left behind. Both are empty outside CI.
variable "prow_build_id" {
  type        = string
  description = "Prow BUILD_ID of the run creating this infra"
  default     = ""
}

variable "prow_pull_number" {
  type        = string
  description = "Pull request number the run belongs to"
  default     = ""
}
