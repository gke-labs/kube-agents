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
  description = "Name of the cluster the scenario is planted on; arrives as TF_VAR_host_cluster_name from the runner."
}

variable "host_cluster_location" {
  type        = string
  description = "Region or zone of host_cluster_name."
}

variable "project_id" {
  type        = string
  description = "GCP project holding host_cluster_name; empty means the gcloud default project."
  default     = ""
}

variable "namespace" {
  type        = string
  description = "The namespace Config Sync keeps every RootSync in. The case's checks name the same namespace."
  default     = "config-management-system"
}

variable "target_resource_name" {
  type        = string
  description = "Name of the planted RootSync. The case's objective names it."
  default     = "root-sync"
}

variable "reconciler_state" {
  type        = string
  description = "What the planted RootSync's reconciler is doing: `stalled` plants the status Config Sync writes when the reconciler cannot come up; `out-of-memory` plants a sync object that reads healthy beside a reconciler pod that is killed for memory in a loop."
  default     = "stalled"
  validation {
    condition     = contains(["stalled", "out-of-memory"], var.reconciler_state)
    error_message = "reconciler_state must be stalled or out-of-memory."
  }
}

variable "memory_limit_mib" {
  type        = number
  description = "Memory limit of the stand-in reconciler container, in MiB (out-of-memory state). Must be below allocate_mib."
  default     = 64
}

variable "allocate_mib" {
  type        = number
  description = "What the stand-in reconciler container allocates at each start, in MiB (out-of-memory state). Must exceed memory_limit_mib."
  default     = 96
}

variable "prow_build_id" {
  type        = string
  description = "Prow BUILD_ID of the run creating this infra"
  default     = ""
}

variable "prow_pull_number" {
  type        = string
  description = "Pull request number the run belongs to"
  default     = ""
}
