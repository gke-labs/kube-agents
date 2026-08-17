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

# The deployer scans this directory's *.tf only and never descends into the
# module, so every variable that has to reach it is re-declared here.

variable "infra_provider" {
  type        = string
  description = "The target cloud provider (gcp, kind)"
}

variable "cluster_name" {
  type        = string
  description = "Name of the cluster to provision"
}

variable "location" {
  type        = string
  description = "Region/zone (GCP) or 'local' (KinD)"
  default     = ""
}

variable "node_count" {
  type        = number
  description = "Number of worker nodes"
  default     = 1
}

variable "machine_type" {
  type        = string
  description = "VM instance type"
  default     = "g2-standard-4"
}

variable "project_id" {
  type        = string
  description = "GCP Project ID"
  default     = ""
}

# Relative to the directory tofu runs in. See tf/prebuilt/kind/variables.tf for
# why this is not ~/.kube/config.
variable "kubeconfig_path" {
  type        = string
  description = "Target path to write kubeconfig (KinD-only)"
  default     = "./kind-kubeconfig.yaml"
}

# Identify the CI run that created this infra so a janitor can find what a run
# killed before teardown left behind. tofu reads these from TF_VAR_* in the
# environment; both are empty outside CI.
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
