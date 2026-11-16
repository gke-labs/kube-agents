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
# bench/tasks/fleet-audit-unchanged-manifest-skips-synthesis/task.yaml are a
# matched pair.

variable "host_cluster_name" {
  type        = string
  description = "Name of the cluster the kube-agents install runs on; the previous run is planted in its agent's report store and the scheduled run is fired there."
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
  description = "Deployment running the Platform Agent pod, which holds the cron roster and the scope snapshot"
  default     = "platform-agent-gateway"
}

variable "agent_container" {
  type        = string
  description = "Container inside agent_deployment that runs hermes"
  default     = "platform-agent"
}

variable "mark" {
  type        = string
  description = "The word that starts each planted title, recommendation and note. The teardown removes the stored envelopes that hold it."
}

variable "own_wait" {
  type        = number
  description = "Seconds to wait for a run of the drift job already claimed or running to finish before planting"
  default     = 1800
}

variable "run_wait" {
  type        = number
  description = "Seconds to wait for the fired scheduled run to end; a full drift audit on the eval fleet takes up to the audit band"
  default     = 2700
}
