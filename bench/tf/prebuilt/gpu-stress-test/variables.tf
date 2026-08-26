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

# Set via TF_VAR_reuse_existing_cluster by hack/ci-eval-pr.sh when the leased
# project carries the standing seeded fleet (bench/tf/fleet): the stack then
# creates no cluster, plants its Cloud Logging fixture against the existing
# cluster named by `cluster_name`/`location`, and destroy leaves that cluster
# standing. false keeps the per-run cluster for a project without a fleet.
variable "reuse_existing_cluster" {
  type        = bool
  description = "Create no cluster; treat cluster_name/location as an existing cluster the log fixture refers to."
  default     = false
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

# General-purpose on purpose. The node runs the GKE system pods and nothing
# else -- the incident this stack stands up is seeded into Cloud Logging, not
# scheduled onto the cluster -- so an accelerator machine family bought
# nothing but cost and a stockout surface. Keep this off the g2-*/a2-*
# families: the gke submodule infers a GPU from the machine family alone
# (`local.is_g2`), so putting one back here re-attaches an L4 silently.
variable "machine_type" {
  type        = string
  description = "VM instance type"
  default     = "e2-standard-4"
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
