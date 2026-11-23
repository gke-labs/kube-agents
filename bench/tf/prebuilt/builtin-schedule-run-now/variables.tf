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
# raises on a task.yaml variable it does not declare. A case on this stack
# passes `schedule` and may pass the waits.

variable "host_cluster_name" {
  type        = string
  description = "Name of the cluster the kube-agents install runs on; the schedule is fired there."
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
  description = "Namespace of the kube-agents install on host_cluster_name, where its AgentSchedules are"
  default     = "kubeagents-system"
}

variable "agent_deployment" {
  type        = string
  description = "Deployment running the gateway pod, whose agent container holds the state file"
  default     = "platform-agent-gateway"
}

variable "agent_container" {
  type        = string
  description = "Container inside agent_deployment that mounts the agent's data volume"
  default     = "platform-agent"
}

variable "schedule" {
  type        = string
  description = "Name of the built-in AgentSchedule to fire, in agent_namespace. The stack refuses a schedule whose kubeagents.x-k8s.io/origin label is not builtin."

  validation {
    condition     = can(regex("^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$", var.schedule))
    error_message = "schedule must be an AgentSchedule name: a DNS-1123 label."
  }
}

variable "idle_wait" {
  type        = number
  description = "Seconds to wait for a run of the schedule already in flight or claimed to settle before firing"
  default     = 1800
}

variable "run_wait" {
  type        = number
  description = "Seconds to wait for the fired run to end; a full audit on the eval fleet takes up to the audit band"
  default     = 2700
}

variable "delivery_wait" {
  type        = number
  description = "Seconds to wait, after the run ends, for the gateway to say how its report was delivered; the operator asks for ten minutes after a run ends"
  default     = 660
}
