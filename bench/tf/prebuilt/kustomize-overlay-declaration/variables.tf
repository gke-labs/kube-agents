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
# bench/tasks/obtainability-pdb-kustomize-overlay/task.yaml are a matched
# pair.

variable "host_cluster_name" {
  type        = string
  description = "Name of the cluster the kube-agents install runs on; only passed through to the cluster_name output"
}

variable "host_cluster_location" {
  type        = string
  description = "Region or zone of host_cluster_name"
}

# Exported to this unit only, because the stack declares the marker below.
variable "gitops_repo" {
  type        = string
  description = "owner/name of the GitOps repository the audit writes to"
  default     = ""
}

# The marker hack/ci-eval-pr.sh reads (BENCH_GITOPS_WRITE_MARKER): declaring
# this variable makes it mint the unit a write token for gitops_repo
# (contents, pull requests and issues write, on that repository only) and pass
# the token's file here. A name of its own, so no other stack gets the token.
variable "bench_gitops_write_token_file" {
  type        = string
  description = "File that holds a GitHub token with write access to gitops_repo"
  default     = ""
}

# The pool repositories' layout: scripts/provision_ci_pool_project.sh seeds
# checkout-gateway.yaml here. A dev fleet applied with a cluster prefix sets
# clusters/<prefix>-seeded-a/seeded-reliability.
variable "cluster_directory" {
  type        = string
  description = "Directory of the checkout-gateway declaration, where the kustomization is planted"
  default     = "clusters/seeded-a/seeded-reliability"
}
