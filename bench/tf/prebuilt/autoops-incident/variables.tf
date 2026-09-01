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

# A variable rather than a literal for the same reason as the two above: the
# watcher is a peer process inside this container, not a container of its own,
# so a rename in deploy/ would otherwise surface here as a 300s timeout blaming
# the watcher rather than as an error naming the container.
variable "agent_container" {
  type        = string
  description = "Container inside agent_deployment that k8s-event-watcher runs alongside"
  default     = "envoy-credential-proxy"
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
# project per run so no two runs share a host cluster, and the runner's task
# loop is sequential. Nor can the watcher's 24h dedup window suppress a second
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
# limit fits it -- and the lettered-options objective below would then be
# asking the agent to pad its answer. Keep the allocation bounded and keep it
# above the limit.
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
