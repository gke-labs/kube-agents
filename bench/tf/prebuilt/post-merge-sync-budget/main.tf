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

# The scenario driver for bench/tasks/post-merge-verified-fixed.
#
# The seeded fleet's post-merge fixture is budgeted at steady state:
# order-ledger-pdb.yaml is merged in the leased GitOps repository and Config
# Sync on seeded-a keeps it applied (bench/tf/fleet/post-merge-sync.tf). The
# apply removes the file and waits for Config Sync to prune the budget, so the
# agent's audit finds order-ledger unbudgeted; the destroy puts the seeded file
# back and waits for the budget to be applied again. budget.py has the steps.
#
# The budget is off only while this stack is applied, and the case runs in the
# fan-out's writers phase, one unit at a time after every other unit
# (hack/ci-eval-pr.sh, unit_phase: the case requests a pull request), so no
# other case in the lease reads the namespace meanwhile.
#
# A failed apply restores before it exits, because Terraform skips the destroy
# provisioner of a resource whose create provisioner failed. A run killed
# before its destroy leaves the file off the branch: the fixture-state scan
# reads the role as drifted, and scripts/provision_ci_pool_post_merge_sync.sh
# is the repair.
#
# Inputs. Because this stack declares bench_gitops_write_token_file, the
# marker of a writing stack, hack/ci-eval-pr.sh mints the unit a write token
# narrowed to the leased repository and exports TF_VAR_gitops_repo, for this
# unit only. budget.py also reads BENCH_FLEET_KUBECONFIG_DIR, which the runner
# exports, for the fixture role's read-only kubeconfig.
#
# Isolation class: one file on the GitOps repository's default branch, removed
# and restored; one budget Config Sync prunes and applies in seeded-post-merge.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0.0"
    }
  }
}

locals {
  # The settings budget.py reads, the same for the apply and the destroy.
  # Destroy-time provisioners may read only `self`, so the triggers carry them.
  settings = {
    POST_MERGE_GITOPS_REPO       = var.gitops_repo
    POST_MERGE_DIRECTORY         = var.cluster_directory
    POST_MERGE_GITOPS_TOKEN_FILE = var.bench_gitops_write_token_file
  }
}

resource "null_resource" "budget_off" {
  triggers = merge(local.settings, {
    script = "${path.module}/budget.py"
    digest = filesha256("${path.module}/budget.py")
  })

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = local.settings
    command     = <<-EOT
      set -euo pipefail
      python3 "${path.module}/budget.py" remove
    EOT
  }

  # No on_failure = continue: a restore that fails exits non-zero, so the
  # destroy reports it, and the fixture-state scan reads the role as drifted.
  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]
    environment = {
      POST_MERGE_GITOPS_REPO       = self.triggers.POST_MERGE_GITOPS_REPO
      POST_MERGE_DIRECTORY         = self.triggers.POST_MERGE_DIRECTORY
      POST_MERGE_GITOPS_TOKEN_FILE = self.triggers.POST_MERGE_GITOPS_TOKEN_FILE
    }
    command = <<-EOT
      set -euo pipefail
      python3 "${self.triggers.script}" restore
    EOT
  }
}

# devops-bench reads these two outputs after up() and points the ambient
# kubeconfig at this cluster. The case reads the seeded fleet through the
# fixture-role kubeconfigs, so the install's host cluster is the one given.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
