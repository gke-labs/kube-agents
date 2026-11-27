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
  description = "Name of the cluster the kube-agents install runs on; the plant is written from its gateway pod."
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

variable "audit_id" {
  type        = string
  description = "The audit stream whose broker store gets the plant. Only compliance-audit: the planted finding is a NetworkPolicy check from its roster."
  default     = "compliance-audit"
  validation {
    condition     = var.audit_id == "compliance-audit"
    error_message = "audit_id is compliance-audit, the one stream whose roster has the planted finding's check."
  }
}

variable "mark" {
  type        = string
  description = "A DNS-1123 label carried by the planted cluster, project and namespace, which a case's reply check looks for"
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$", var.mark))
    error_message = "mark is a DNS-1123 label of 3 to 32 characters."
  }
}

variable "gitops_repo" {
  type        = string
  description = "owner/name of the GitOps repository; empty resolves it the way audit_report.py does. Only its characters are checked here, so it can't carry shell syntax; audit_report.py checks its shape at apply, and a seed that refuses it runs teardown."
  default     = ""
  validation {
    condition     = can(regex("^[A-Za-z0-9._/-]*$", var.gitops_repo)) && !can(regex("(^|/)[.][.]?(/|$)", var.gitops_repo))
    error_message = "gitops_repo holds only letters, digits, '.', '_', '-' and '/', with no '.' or '..' segment."
  }
}
