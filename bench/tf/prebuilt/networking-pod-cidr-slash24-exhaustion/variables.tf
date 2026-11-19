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
# bench/tasks/networking-pod-cidr-slash24-exhaustion/task.yaml: the prompt does
# not name it (the audit has to find the range), but the objective asserts on
# the GKE-created range name gke-<prefix>-<hash>-pods-<hash>. Change one and
# change both.
# ---------------------------------------------------------------------------
variable "cluster_name_prefix" {
  type        = string
  description = "Prefix of the planted cluster's name, and so of its GKE-created Pod range"
  default     = "bench-podrange"
}

# A netmask alone: GKE picks a free block of that size on the default network
# and creates the secondary range itself. /21 is the smallest GKE takes; it
# holds eight per-node /24 blocks at 110 Pods a node.
variable "pod_range_netmask" {
  type        = string
  description = "Netmask of the Pod secondary range GKE creates for the planted cluster"
  default     = "/21"
  validation {
    condition     = can(regex("^/[0-9]{1,2}$", var.pod_range_netmask))
    error_message = "pod_range_netmask must be a bare netmask such as /24."
  }
}

# All eight of the /21 range's per-node blocks: 100% allocated. Seven would
# read 87.5%, still under the check's 15% floor of free space; six would read
# 75% and file nothing.
variable "node_count" {
  type        = number
  description = "Nodes in the planted pool; each takes one /24 block of the Pod range"
  default     = 8
}

# GKE reports no utilization for a Services range, so the audit measures
# nothing here; sized well clear of the /27 minimum.
variable "services_range_netmask" {
  type        = string
  description = "Netmask of the Services secondary range GKE creates for the planted cluster"
  default     = "/24"
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
