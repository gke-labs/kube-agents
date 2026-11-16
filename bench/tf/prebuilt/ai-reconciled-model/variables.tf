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
# file does not declare is dropped silently — the matched-pair rule
# prebuilt/autoops-incident/variables.tf states.

# The cluster the Platform Agent runs in, for prebuilt/ai-model-defects'
# reason: the audit sweeps the leased project's clusters, and the host
# cluster is the one cluster every eval project has.
variable "host_cluster_name" {
  type        = string
  description = "Name of the cluster the AI workload is planted on."
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

# ---------------------------------------------------------------------------
# The planted workload. Every value below is also written into
# bench/tasks/ai-security-manual-names-reconciler/task.yaml, whose objectives
# and safeguards assert on them. Change one and change both.
# ---------------------------------------------------------------------------

variable "defect_namespace" {
  type        = string
  description = "Namespace the AI workload is planted in, and torn down with"
  default     = "eval-ai-reconciled"
}

variable "workload_name" {
  type        = string
  description = "Name of the planted model-serving Deployment"
  default     = "eval-reconciled-model"
}

# Matches AI_MODEL_IMAGE_RE through `/vllm/`, floats on `:latest`, and names
# a registry host that never resolves.
variable "model_image" {
  type        = string
  description = "Image of the planted Deployment: an AI model server on a floating tag"
  default     = "registry.invalid/vllm/vllm-openai:latest"
}

# The Helm release the two meta.helm.sh annotations name. The collector reads
# it as "the Helm release `<name>` in <namespace>", and the case grades that
# phrase in the ledger body.
variable "helm_release_name" {
  type        = string
  description = "Helm release named by the Deployment's meta.helm.sh annotations"
  default     = "eval-model-release"
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
