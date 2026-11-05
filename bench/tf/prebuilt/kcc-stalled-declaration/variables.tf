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
# raises on a task.yaml variable it does not declare, so this file and the
# task file are a matched pair.

variable "project_id" {
  type        = string
  description = "GCP project that holds the seeded fleet and the Config Connector cluster"
  default     = ""
}

# No pool project runs Config Connector today, so these have no default: the
# case is a FIXTURE_NOT_READY entry in scripts/validate_bench_cases.py until one
# does, and the apply refuses an empty value.
variable "kcc_cluster_name" {
  type        = string
  description = "Cluster that runs Config Connector, where the stalled object is planted"
  default     = ""
}

variable "kcc_cluster_location" {
  type        = string
  description = "Region or zone of kcc_cluster_name"
  default     = ""
}

# The namespace is the stack's own: the plant refuses one it did not label,
# and the teardown deletes only one it labelled. Repeated in the task's
# required phrases and safeguard.
variable "kcc_namespace" {
  type        = string
  description = "Namespace the stalled object is planted in, and deleted with"
  default     = "eval-kcc-stalled"
}

# The seeded pool the stockout audit files a finding on (the hpa-saturated
# role): single-zone, autoscaling capped at one node. The declaration names it,
# so the audit's fix for it edits the planted file.
variable "seeded_slot" {
  type        = string
  description = "Seeded-fleet slot whose cluster holds seeded_pool"
  default     = "a"
}

variable "seeded_pool" {
  type        = string
  description = "Node pool the planted declaration names"
  default     = "pinned-inference-pool"
}

variable "gitops_repo" {
  type        = string
  description = "owner/name of the GitOps repository the audit writes to"
  default     = ""
}

# Required when Config Connector runs in namespaced mode on kcc_cluster_name:
# the plant then creates a ConfigConnectorContext in kcc_namespace with this
# service account, because no controller watches a namespace without one. The
# account needs no IAM role: the planted object stops at DependencyNotFound
# before any call to GCP. Not used in cluster mode.
variable "kcc_google_service_account" {
  type        = string
  description = "Service account email for the ConfigConnectorContext in namespaced mode"
  default     = ""
}

# Contents read and write, and pull requests write, on gitops_repo. The name
# is the marker by which the bench harness will give a stack the narrowed
# GitOps write token: only a stack that declares this variable gets
# TF_VAR_bench_gitops_write_token_file, with TF_VAR_gitops_repo,
# TF_VAR_kcc_cluster_name, TF_VAR_kcc_cluster_location and
# TF_VAR_kcc_google_service_account. That harness behaviour depends on
# #4742, which is not merged; until it is, nothing sets these variables.
# Empty uses BENCH_GITHUB_TOKEN from the environment.
variable "bench_gitops_write_token_file" {
  type        = string
  description = "File that holds the GitHub write token for gitops_repo"
  default     = ""
}
