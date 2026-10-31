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

# The deployer drops an injected variable this stack does not declare and
# raises on a task.yaml variable it does not declare, so this file and
# bench/tasks/fleet-audit-chat-report-is-the-report/task.yaml are a matched pair.

variable "host_cluster_name" {
  type        = string
  description = "Name of the cluster the Platform Agent runs in; the audit is fired there."
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
  description = "Deployment running the Platform Agent pod, which holds the cron roster"
  default     = "platform-agent-gateway"
}

variable "agent_container" {
  type        = string
  description = "Container inside agent_deployment that runs hermes"
  default     = "platform-agent"
}

variable "sandbox_pod" {
  type        = string
  description = "The shell sandbox pod, where the audit's report store and in-flight note live"
  default     = "platform-agent-shell-0"
}

variable "sandbox_container" {
  type        = string
  description = "Container inside sandbox_pod that runs the agent's shell"
  default     = "shell"
}

variable "audit_job" {
  type        = string
  description = "The fleet-audit cron job to fire; its id is also its audit stream id"
  default     = "compliance-audit"
}

variable "own_wait" {
  type        = number
  description = "Seconds to wait for a run of the job already claimed or running to finish"
  default     = 3600
}

variable "run_wait" {
  type        = number
  description = "Seconds to wait for the fired run to end"
  default     = 4800
}
