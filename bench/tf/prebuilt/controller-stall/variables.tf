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

variable "host_cluster_name" {
  type        = string
  description = "Name of the cluster the Platform Agent runs in; the stall is planted here."
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
  description = "Deployment running the Platform Agent pod that hosts the Session KV daemon"
  default     = "platform-agent-gateway"
}

variable "agent_container" {
  type        = string
  description = "Container inside agent_deployment carrying the Session KV daemon, its API key and the image's stall_report.py"
  default     = "platform-agent"
}

variable "daemon_url" {
  type        = string
  description = "Base URL of the Session KV daemon, as seen from inside the agent pod"
  default     = "http://127.0.0.1:8699" # sanitizer: allow the daemon's own loopback bind, reachable only from inside the agent pod
}

variable "stall_namespace" {
  type        = string
  description = "Namespace the stalled workload is planted in, and torn down with"
  default     = "eval-controller-stall"
}

variable "workload_name" {
  type        = string
  description = "Name of the Deployment whose rollout never finishes"
  default     = "eval-stalled-workload"
}

variable "readiness_gate" {
  type        = string
  description = "Pod readiness gate condition type that nothing in the cluster ever sets"
  default     = "eval.example.com/cache-primed"
}

variable "progress_deadline_seconds" {
  type        = number
  description = "The Deployment's progressDeadlineSeconds, short so Progressing turns False well inside the scan's threshold"
  default     = 60
}

variable "scan_ceiling_seconds" {
  type        = number
  description = "How long to wait for stall_report.py to report the Deployment. Its Deployment threshold is 10 minutes, so this is that plus margin."
  default     = 900
}

variable "card_ceiling_seconds" {
  type        = number
  description = "How long to wait for the triage card to finish once it is filed"
  default     = 900
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
