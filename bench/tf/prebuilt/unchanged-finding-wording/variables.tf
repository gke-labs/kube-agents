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
# the task.yaml of each case on this stack are matched
# (fleet-audit-unchanged-finding-keeps-wording, fleet-audit-remediation-right-and-not-stale,
# fleet-audit-wrong-kept-remediation-gives-way).

variable "host_cluster_name" {
  type        = string
  description = "Name of the cluster the kube-agents install runs on; the previous run is planted in the report store of its agent."
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

variable "mark" {
  type        = string
  description = "The word that starts each planted title, impact, recommendation and note. A case looks for it in the published ledger or in the stored report."
}

variable "planted_remediation" {
  type        = string
  description = "What the planted authorized-networks finding proposes: manual (a marked note), gcloud-stale (a marked gcloud command with a literal range, on evidence that differs from this run's, which finish must not carry), or gcloud-same (the same command on equal evidence: finish keeps the planted title and impact, and its content rules make the planted command give way to this run's)."
  default     = "manual"

  validation {
    condition     = contains(["manual", "gcloud-stale", "gcloud-same"], var.planted_remediation)
    error_message = "planted_remediation is one of manual, gcloud-stale, gcloud-same."
  }
}

variable "collector_args" {
  type        = string
  description = "Arguments the plant passes to the drift collector. Empty on an install that declares no scope; on one that does, the scope flags the fleet_scope tool gives (for example --scope-projects <project>), without which the collector sweeps nothing and the plant has no candidate."
  default     = ""
}
