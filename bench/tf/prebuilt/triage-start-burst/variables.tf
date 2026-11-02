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
# reach the stack is declared here. A variable named in the task file's
# `variables:` block and not declared here raises ConfigError; an injected one
# this stack does not declare is dropped. The task file and this one are a
# matched pair (bench/tf/prebuilt/autoops-incident/variables.tf says the same).

# The cluster the Platform Agent pod runs in, and the only cluster whose events
# k8s-event-watcher is sure to be watching inside the run; main.tf's header and
# bench/tf/prebuilt/autoops-incident/main.tf say why. hack/ci-eval-pr.sh passes
# both from HOST_CLUSTER_NAME/REGION.
variable "host_cluster_name" {
  type        = string
  description = "Name of the cluster the Platform Agent runs in; the burst is planted here."
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

# Where the install lives, so the wait can read the watcher's own log.
# hack/ci-eval-pr.sh exports TF_VAR_agent_namespace from TARGET_NAMESPACE.
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

# k8s-event-watcher is a peer process inside this container, not a container of
# its own (deploy/shared/start-services.sh).
variable "agent_container" {
  type        = string
  description = "Container inside agent_deployment that k8s-event-watcher runs alongside"
  default     = "envoy-credential-proxy"
}

# ---------------------------------------------------------------------------
# The planted burst. The namespace and the pod label are also written into
# bench/tasks/triage-start-burst/task.yaml (the triage_starts check and the
# safeguard). Change one and change both.
# ---------------------------------------------------------------------------

# Static, like autoops-incident's: no other task names it, and the verifier
# joins ledger rows to this run's pods by pod UID, so an earlier run's rows in
# the same namespace are not graded.
variable "burst_namespace" {
  type        = string
  description = "Namespace the burst pods are planted in, and torn down with"
  default     = "eval-triage-burst"
}

# Pod names are <prefix>-<index>, and the prefix is also the pods' `app` label,
# which the task's safeguard selects on.
variable "pod_name_prefix" {
  type        = string
  description = "Name prefix and app label of the planted pods"
  default     = "eval-burst-pod"
}

# How many Warning alerts arrive at once. 20 is the burst that lost 10 triage
# starts on an install whose daemon makes one session attempt per alert.
variable "pod_count" {
  type        = number
  description = "Number of pods planted at once, each raising one image-pull Warning"
  default     = 20

  validation {
    condition     = var.pod_count >= 1 && var.pod_count <= 100
    error_message = "pod_count must be between 1 and 100."
  }
}

# Bounds the wait for the watcher to fire for every planted pod.
variable "fire_wait_seconds" {
  type        = number
  description = "Seconds to wait for k8s-event-watcher to log a fire line for every planted pod"
  default     = 300
}

variable "agent_cli_container" {
  type        = string
  description = "Container inside agent_deployment that carries the hermes CLI, for archiving the burst's cards at destroy"
  default     = "platform-agent"
}

variable "start_settle_seconds" {
  type        = number
  description = "Seconds to wait after the last fire before the agent turn: the product's queue limit (TRIAGE_QUEUE_WAIT_LIMIT_S in session_kv_server.py), so a start it calls on time is graded as started"
  default     = 1200
}

# Identify the CI run that planted this, so a janitor can find what a killed
# run left behind. Both are empty outside CI.
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
