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
  description = "Name of the cluster the kube-agents install runs on; the request is written into its agent pod."
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
  description = "Deployment running the agent pod whose chat profile greets"
  default     = "platform-agent-gateway"
}

variable "agent_container" {
  type        = string
  description = "Container inside agent_deployment running the chat profile's gateway"
  default     = "platform-agent"
}

variable "case_suffix" {
  type        = string
  description = "Appended to the marker name so concurrent cases write separate files"

  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.case_suffix))
    error_message = "case_suffix must be lowercase letters, digits and hyphens."
  }
}

variable "variant" {
  type        = string
  description = "Which greeting the plugin injects: in_progress (scan running) or completed (scan done)"

  validation {
    condition     = contains(["in_progress", "completed"], var.variant)
    error_message = "variant must be in_progress or completed."
  }
}

variable "phrase" {
  type        = string
  description = "Substring of the case's prompt and of no other case's; only a turn containing it gets the greeting"

  # The plugin matches by substring on every turn, so a short phrase (" ", "hi")
  # would greet any concurrent case's turn. scripts/validate_bench_cases.py
  # checks the same floor and that the phrase is in the case's own prompt.
  validation {
    condition     = length(trimspace(var.phrase)) >= 12
    error_message = "phrase must be at least 12 characters after trimming."
  }
}
