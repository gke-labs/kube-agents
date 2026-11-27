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
# bench/tasks/post-merge-verified-fixed/task.yaml are a matched pair.

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
  description = "owner/name of the GitOps repository Config Sync reads the post-merge fixture from"
  default     = ""
}

# The marker hack/ci-eval-pr.sh reads (BENCH_GITOPS_WRITE_VARIABLE): declaring
# this variable makes it mint the unit a write token for gitops_repo and pass
# the token's file here.
variable "bench_gitops_write_token_file" {
  type        = string
  description = "File that holds a GitHub token with write access to gitops_repo"
  default     = ""
}

# seeded-a's real name, which hack/ci-eval-pr.sh exports to every writing
# stack's unit from the fleet context (export_writing_stack_inputs, KCC_FLEET_SLOT
# "a"): `<cluster_prefix>-a`, `seeded-a` on every pool project. Config Sync
# syncs clusters/<it>/seeded-post-merge (bench/tf/fleet/post-merge-sync.tf).
variable "kcc_cluster_name" {
  type        = string
  description = "Name of the seeded fleet's slot-a cluster, whose directory Config Sync syncs into seeded-post-merge"
  default     = "seeded-a"
}

# An explicit directory, for a run outside the runner; empty derives it from
# kcc_cluster_name.
variable "cluster_directory" {
  type        = string
  description = "Directory Config Sync syncs into seeded-post-merge, which holds order-ledger's budget at steady state; empty is clusters/<kcc_cluster_name>/seeded-post-merge"
  default     = ""
}
