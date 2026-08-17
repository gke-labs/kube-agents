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

variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "location" {
  description = "The GCP zone or region"
  type        = string
  default     = "us-central1-a"
}

variable "cluster_name" {
  description = "The name of the GKE cluster"
  type        = string
}

variable "node_count" {
  description = "Number of nodes in the standard pool"
  type        = number
  default     = 3
}

variable "machine_type" {
  description = "Machine type for the nodes"
  type        = string
  default     = "e2-standard-2"
}

variable "enable_workload_identity" {
  description = "Enable GKE Workload Identity"
  type        = bool
  default     = false
}

variable "kubernetes_version" {
  description = "The Kubernetes version for the GKE cluster"
  type        = string
  default     = null
}

variable "agent_service_account" {
  description = "The service account email of the agent"
  type        = string
  default     = ""
}

variable "enable_iap_ssh" {
  description = "Enable IAP SSH firewall rule for the cluster"
  type        = bool
  default     = false
}

variable "gpu_type" {
  description = "Abstract GPU family: 'l4', 'a100', 't4', or '' for no GPU"
  type        = string
  default     = ""

  validation {
    condition     = contains(["", "l4", "a100", "t4"], var.gpu_type)
    error_message = "gpu_type must be one of: '', 'l4', 'a100', 't4'."
  }
}

variable "gpu_count" {
  description = "Quantity of GPUs to attach per node"
  type        = number
  default     = 1
}

