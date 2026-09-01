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
# reach the stack is declared here — the same matched-pair rule
# prebuilt/autoops-incident/variables.tf states, and this stack is that one's
# sibling: same host cluster, same watcher, a different failure class. A
# SEPARATE DIRECTORY rather than a variable on the sibling, deliberately:
# devops-bench applies a stack in place, so two tasks sharing one directory
# would share one Terraform state, and the second task's apply would adopt —
# and its destroy would tear down — the first task's objects.

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
  type        = string
  description = "Container inside agent_deployment that k8s-event-watcher runs alongside"
  default     = "envoy-credential-proxy"
}

# ---------------------------------------------------------------------------
# The planted incident. Every value below is also written into
# bench/tasks/autoops-crashloop-config-triage/task.yaml — in its prompt, which
# names the namespace the agent looks for, and in its safeguards, which assert
# the crashing command came out unchanged. Change one and change both.
#
# Static names, not per-BUILD_ID, for the sibling stack's reasons: Boskos
# leases one project per run, and the watcher's 24h dedup key is
# {pod UID, reason}, which every fresh Deployment re-mints. Distinct from the
# sibling's names so the two incidents' cards can never be confused when both
# tasks run in one presubmit.
# ---------------------------------------------------------------------------

variable "incident_namespace" {
  type        = string
  description = "Namespace the incident workload is planted in, and torn down with"
  default     = "eval-autoops-crashloop"
}

# Carries no diagnostic content on purpose — the sibling's rule: a name like
# "eval-badconfig" would let a report satisfy the root-cause objective by
# transcribing the title.
variable "workload_name" {
  type        = string
  description = "Name of the Deployment whose container crash-loops on a missing config file"
  default     = "eval-startup-workload"
}

# The planted evidence noun. The container's only command reads this path,
# fails, and prints it into the container log — nowhere else. A triage that
# names it read the evidence (the log or the command in the spec); one that
# pattern-matched "crashloop means OOM" cannot produce it. The task's
# objective requires the basename below verbatim, so change one and change
# both.
variable "missing_config_path" {
  type        = string
  description = "Absolute path the container tries to read and crash on; its basename is the planted evidence noun"
  default     = "/etc/eval-startup-conf/eval-missing-config.yaml"
}

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
