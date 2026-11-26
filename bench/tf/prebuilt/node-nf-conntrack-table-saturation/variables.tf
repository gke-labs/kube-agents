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

# The deployer scans this directory's *.tf only; a variable a task names and
# this file does not declare raises ConfigError, and an injected one this
# file does not declare is dropped silently: the matched-pair rule
# prebuilt/autoops-incident/variables.tf states.

variable "project_id" {
  type        = string
  description = "GCP project the planted cluster is created in; the harness reads it to fetch the cluster's credentials"
}

# The runner's per-run task-cluster name (TF_VAR_cluster_name). Not used as a
# resource name; main.tf hashes it into this stack's own names.
variable "cluster_name" {
  type        = string
  description = "Per-run identity the stack's resource names are derived from"
}

# The runner's zone (GCP_LOCATION). The planted cluster is zonal.
variable "location" {
  type        = string
  description = "Zone the planted cluster is created in"
  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]+-[a-z]$", var.location))
    error_message = "location must be a zone such as us-west4-a."
  }
}

# ---------------------------------------------------------------------------
# The cluster name prefix is also written into
# bench/tasks/node-nf-conntrack-table-saturation/task.yaml: the prompt does
# not name it (the audit has to find the node), but the objective asserts on
# the finding's object NodePool/<zone>/<prefix>-<hash>/default-pool. Change
# one and change both.
# ---------------------------------------------------------------------------
variable "cluster_name_prefix" {
  type        = string
  description = "Prefix of the planted cluster's name, and so of the node pool the finding names"
  default     = "bench-conntrack"
}

# The kernel's line was in Cloud Logging within 90 seconds in the live tests.
# The wait also covers the pod's image pull and its burst.
variable "kernel_line_timeout_seconds" {
  type        = number
  description = "Seconds the apply waits for the kernel's line from the planted node to be in Cloud Logging before it fails"
  default     = 600
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
