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
  description = "Name of the cluster the Platform Agent runs in; the report store lives on its shell sandbox."
}

variable "host_cluster_location" {
  type        = string
  description = "Region or zone of host_cluster_name."
}

variable "project_id" {
  type        = string
  description = "GCP project holding host_cluster_name and the seeded fleet the report names."
  default     = ""
}

variable "agent_namespace" {
  type        = string
  description = "Namespace of the kube-agents install on host_cluster_name."
  default     = "kubeagents-system"
}

variable "sandbox_pod" {
  type        = string
  description = "The shell sandbox pod, where the upgrade retrospective's report store lives."
  default     = "platform-agent-shell-0"
}

variable "sandbox_container" {
  type        = string
  description = "Container inside sandbox_pod that runs the agent's shell."
  default     = "shell"
}

variable "gate_mode" {
  type        = string
  description = "The gate the first-upgraded cluster's report carries: risk (one container's log volume rose tenfold past the floor) or clear."
  default     = "risk"
  validation {
    condition     = contains(["risk", "clear"], var.gate_mode)
    error_message = "gate_mode must be risk or clear."
  }
}

variable "seeded_label_filter" {
  type        = string
  description = "The clusters list filter that selects the seeded fleet; the slot is the name's last segment (-a, -b, -c, -d)."
  default     = "resourceLabels.environment=seeded"
}

variable "store_dir" {
  type        = string
  description = "The retrospective's report store on the sandbox (upgrade_retrospective.py: DEFAULT_STORE_DIR/REPORTS_SUBDIR)."
  default     = "/opt/data/upgrade-retrospective/reports"
}
