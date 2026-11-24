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
  description = "Name of the cluster the Platform Agent runs in"
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

# The report is planted on the shell sandbox's data volume, where onboarding's
# ranking worker writes the real one and where get_first_run_report reads. The
# apply refuses unless exactly one pod matches.
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

# The plant and the restore run as this login, the one the model's terminal
# uses, not as the container's root: /opt/data is that login's, so nothing the
# model places there can turn a write by this stack onto anything it could not
# write itself.
variable "sandbox_user" {
  type        = string
  description = "Login inside the shell sandbox that owns /opt/data (the terminal's ssh_user)"
  default     = "agent"
}

variable "workload_namespace" {
  type        = string
  description = "Namespace the planted report's two critical findings name. Nothing is planted in it; the task's objective requires both workload names in the reply."
  default     = "eval-first-run"
}

variable "workload_names" {
  type        = list(string)
  description = "The two workloads the planted report names as its criticals, in order. Repeated in the task's objective."
  default     = ["ledger-writer", "queue-drainer"]
}
