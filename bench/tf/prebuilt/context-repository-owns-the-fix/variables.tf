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
# raises on a task.yaml variable it does not declare, so this file and
# bench/tasks/remediation-context-repository-owns-the-fix/task.yaml are a
# matched pair.

variable "host_cluster_name" {
  type        = string
  description = "Name of the cluster the kube-agents install runs on; the workload is planted here, and the GitOps directory is clusters/<this name>/"
}

variable "host_cluster_location" {
  type        = string
  description = "Region or zone of host_cluster_name"
}

variable "project_id" {
  type        = string
  description = "GCP project that holds host_cluster_name; the deployer injects it from PROJECT_ID"
  default     = ""
}

variable "agent_namespace" {
  type        = string
  description = "Namespace of the kube-agents install on host_cluster_name"
  default     = "kubeagents-system"
}

variable "agent_name" {
  type        = string
  description = "Name of the PlatformAgent, which names its GitOps state ConfigMap, gateway Deployment and shell StatefulSet"
  default     = "platform-agent"
}

# Exported to this unit only, because the stack declares the marker below.
variable "gitops_repo" {
  type        = string
  description = "owner/name of the GitOps repository the HelmRelease directory is planted in"
  default     = ""
}

# The marker hack/ci-eval-pr.sh reads (BENCH_GITOPS_WRITE_VARIABLE): declaring
# this variable makes it mint the unit a write token for gitops_repo
# (contents, pull requests and issues write, on that repository only) and pass
# the token's file here. A name of its own, so no other stack gets the token.
variable "bench_gitops_write_token_file" {
  type        = string
  description = "File that holds a GitHub token with write access to gitops_repo"
  default     = ""
}

# The context repository: public, so the broker reads it with no credential,
# and its tree is the case's fixture/context-repository/ on this ref
# (fixture/publish_context_repository.py publishes it). The three are what the
# planted source.yaml names and what the registration carries.
variable "context_repository_owner" {
  type        = string
  description = "GitHub owner of the platform charts context repository"
  default     = "gke-agentic"
}

variable "context_repository_name" {
  type        = string
  description = "Name of the platform charts context repository"
  default     = "kube-agents-evals-platform-charts"
}

variable "context_repository_ref" {
  type        = string
  description = "Branch of the context repository that holds the fixture tree; the registration pins the broker's reads to it"
  default     = "main"
}

variable "prow_build_id" {
  type        = string
  description = "Prow BUILD_ID of the run creating this fixture; a label on the planted namespace"
  default     = ""
}

variable "prow_pull_number" {
  type        = string
  description = "Pull request number the run belongs to; a label on the planted namespace"
  default     = ""
}
