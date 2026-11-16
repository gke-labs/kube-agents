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
# bench/tasks/fleet-audit-unread-target-keeps-finding/task.yaml are a matched
# pair.

variable "host_cluster_name" {
  type        = string
  description = "Name of the cluster the kube-agents install runs on. The ledger is opened from its shell sandbox, and the seeded finding is on this cluster."
}

variable "host_cluster_location" {
  type        = string
  description = "Region or zone of host_cluster_name, as the GKE cluster list gives it."
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

# The audit worker runs audit_report.py in the shell sandbox, so the ledger and
# its stored record are written there. The apply refuses unless exactly one pod
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

variable "gitops_repo" {
  type        = string
  description = "owner/name of the GitOps repository; empty resolves it the way audit_report.py does"
  default     = ""
}
