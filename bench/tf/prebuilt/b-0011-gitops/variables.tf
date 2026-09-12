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

# --- cluster (same shape as the other prebuilt stacks) ----------------------

variable "infra_provider" {
  type        = string
  description = "The target cloud provider (gcp, kind)"
}

variable "cluster_name" {
  type        = string
  description = "Name of the cluster to provision. Also the default seed of the run branch name."
}

variable "location" {
  type        = string
  description = "Region/zone (GCP) or 'local' (KinD)"
  default     = ""
}

variable "node_count" {
  type        = number
  description = "Number of worker nodes. One e2-standard-2 fits the b-0011 workloads plus Argo CD core."
  default     = 1
}

variable "machine_type" {
  type        = string
  description = "VM instance type"
  default     = "e2-standard-2"
}

variable "project_id" {
  type        = string
  description = "GCP Project ID"
  default     = ""
}

variable "kubeconfig_path" {
  type        = string
  description = "Kubeconfig the setup script writes credentials into (KinD writes here; GKE via gcloud get-credentials)."
  default     = "~/.kube/config"
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

variable "wait_timeout" {
  type        = string
  description = "Seconds each bounded poll in setup.sh waits before declaring SEED FAIL."
  default     = "180"
}

# devops-bench forwards namespace= to every GCP stack when NAMESPACE is set.
# Declared so that does not trip an undeclared-variable warning; unused here.
variable "namespace" {
  type    = string
  default = "payments"
}

# --- GitOps cycle -------------------------------------------------------------

variable "gitops_repo" {
  type        = string
  description = "HTTPS URL of the leaderboard GitOps repository Argo CD syncs from and the agent opens PRs against."
  default     = "https://github.com/gke-agentic/fuxiao-gkedemo-infra"
}

variable "gitops_task_path" {
  type        = string
  description = "Directory in gitops_repo holding this task's broken base."
  default     = "tasks/b-0011"
}

variable "gitops_broken_base_sha" {
  type        = string
  description = "Commit in gitops_repo that the per-run branch is cut from. Rendered by scripts/render-broken-base.sh; see the plan in gke-labs/kube-agents#1307."
  default     = "a48b227c54f76ee0a1c92a85ddf4d4eab8c4174c"
}

variable "gitops_run_branch" {
  type        = string
  description = "Per-run branch Argo tracks and the agent's PR targets. Empty means run/<cluster_name>/b-0011, which is what the task prompt tells the agent."
  default     = ""
}

variable "gitops_token_file" {
  type        = string
  description = "File holding a GitHub token for gitops_repo: contents read/write on that one repository (branch create/delete, Argo repo access)."
  default     = "~/.config/gitops-pilot/github-token"
}

# Pilot-only fallback. The agent resolves its PR base from GITOPS_BASE_BRANCH
# or, when unset, from the remote's advertised default branch. On an operator
# whose sandbox env allowlist does not carry GITOPS_BASE_BRANCH (releases before
# the change in gke-labs/kube-agents#1307), the run branch can be made the
# repository default for the run and restored on destroy instead. One run at a
# time. Requires "administration" permission on the token.
variable "gitops_switch_default_branch" {
  type        = bool
  description = "Make the run branch the repository's default branch for the run, restoring gitops_restore_default_branch on destroy."
  default     = false
}

variable "gitops_restore_default_branch" {
  type        = string
  description = "Default branch to restore on destroy when gitops_switch_default_branch is set."
  default     = "main"
}

# Onboard the per-run cluster with the platform agent before the agent's turn.
# The platform agent delegates single-cluster work to a Cluster Agent profile
# that must already be scaffolded (kubeconfig pin + USER.md identity). The
# hourly reconcile job is too slow for a per-run cluster, and a worker spawned
# against a missing profile leaves a private (0700) profile directory behind
# that no later scaffold can write into (measured 2026-09-10). When set, setup.sh
# runs the scaffold inside the agent pod; empty skips the step.
variable "agent_host_context" {
  type        = string
  description = "kubectl context of the cluster running the platform agent; empty disables onboarding."
  default     = ""
}

variable "agent_namespace" {
  type        = string
  description = "Namespace of the platform agent Deployment on agent_host_context."
  default     = "kubeagents-system"
}

variable "argocd_version" {
  type        = string
  description = "Argo CD release whose manifests/core-install.yaml is applied to the task cluster."
  default     = "v3.5.2"
}
