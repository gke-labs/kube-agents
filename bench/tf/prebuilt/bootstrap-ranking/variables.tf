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
# raises on a task.yaml variable it does not declare, so this file and the
# task.yaml of each case on this stack (bootstrap-inventory-ranking-delivery,
# bootstrap-inventory-posture-scope) are a matched set.

variable "host_cluster_name" {
  type        = string
  description = "Name of the cluster the Platform Agent runs in; the prioritization card is filed there."
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
  description = "Deployment running the Platform Agent pod, which holds the onboarding markers and the board"
  default     = "platform-agent-gateway"
}

variable "agent_container" {
  type        = string
  description = "Container inside agent_deployment that runs hermes"
  default     = "platform-agent"
}

# The card's worker reads and writes its files through the shell sandbox, so
# the raw report is planted there. The apply refuses unless exactly one pod
# matches.
variable "sandbox_selector" {
  type        = string
  description = "Label selector for the shell sandbox pods in agent_namespace"
  default     = "app=platform-agent-shell"
}

variable "sandbox_container" {
  type        = string
  description = "Container inside the shell sandbox pods"
  default     = "shell"
}

# Which planted report, beside main.tf, the case ranks. Each has a cards file
# beside it that bench/tests/test_bootstrap_ranking.py composes it from.
variable "raw_report" {
  type        = string
  description = "File name, beside main.tf, of the INVENTORY.raw.md this case plants"
  default     = "inventory-raw.txt"
  # The teardown resolves, and the fixture test composes, only files matching this.
  validation {
    condition     = can(regex("^inventory-raw[^/]*\\.txt$", var.raw_report))
    error_message = "raw_report must name an inventory-raw*.txt file beside main.tf."
  }
}
