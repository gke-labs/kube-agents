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
# file does not declare is dropped silently, the matched-pair rule
# prebuilt/autoops-incident/variables.tf states.

# The project the two planted VPCs are created in. It is also the consumer
# project the service attachment's reject list names, which is what makes
# the endpoint read REJECTED, so it cannot fall back to the gcloud default.
variable "project_id" {
  type        = string
  description = "GCP Project ID the planted VPCs, service attachment and endpoint are created in"
  validation {
    condition     = length(var.project_id) > 0
    error_message = "project_id must name the project; the reject list is keyed on it."
  }
}

# The runner's per-run task-cluster name (TF_VAR_cluster_name). Not used as a
# resource name; main.tf hashes it into this stack's own names.
variable "cluster_name" {
  type        = string
  description = "Per-run identity the stack's resource names are derived from"
}

# The runner's zone (GCP_LOCATION). Every planted object is regional, so only
# the zone's region is used.
variable "location" {
  type        = string
  description = "Zone whose region holds the planted objects"
  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]+-[a-z]$", var.location))
    error_message = "location must be a zone such as us-west4-a."
  }
}

# Both arrive from the environment (TF_VAR_host_cluster_name /
# TF_VAR_host_cluster_location, exported by hack/ci-eval-pr.sh), and only
# feed the outputs: the agent's host cluster, which devops-bench points the
# kubeconfig at.
variable "host_cluster_name" {
  type        = string
  description = "Name of the agent's host cluster"
}

variable "host_cluster_location" {
  type        = string
  description = "Region or zone of host_cluster_name"
}

variable "name_prefix" {
  type        = string
  description = "Prefix of the planted producer and consumer VPC, subnet, load balancer and service attachment names"
  default     = "bench-psc"
}

# ---------------------------------------------------------------------------
# The planted endpoint's name prefix is also written into
# bench/tasks/networking-psc-routing-deadlock-baseline/task.yaml: the prompt
# does not name it (the audit has to find it), but the objective asserts on
# it. Change one and change both.
# ---------------------------------------------------------------------------
variable "endpoint_name_prefix" {
  type        = string
  description = "Prefix of the planted endpoint's address and forwarding-rule names"
  default     = "bench-psc-rejected"
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
