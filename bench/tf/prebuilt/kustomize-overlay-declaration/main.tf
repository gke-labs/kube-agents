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

# The scenario driver for bench/tasks/obtainability-pdb-kustomize-overlay.
#
# It plants, for the run only, one file in the leased GitOps repository:
# clusters/seeded-a/seeded-reliability/kustomization.yaml, which lists the
# checkout-gateway.yaml beside it as its only resource. Every pool repository
# holds that declaration, so the directory becomes a Kustomize root, and the
# audit must not open a pull request that adds a file the kustomization does
# not list. plant.py has the steps and the reasons; this file only runs it.
#
# The file is there only while this stack is applied, so the
# obtainability-major-pdb-auto-pr case, which needs the same directory
# without a kustomization, is not changed by it. The case is `writes_alone`,
# so no other unit runs while the file is there.
#
# Teardown on both paths. A failed plant removes what it wrote before it
# exits, because Terraform taints a resource whose create provisioner failed
# and then skips its destroy provisioner. The destroy provisioner removes the
# rest on the success path: first the pull requests the audit opened under the
# directory (labelled `audit:stale-closed` before the close), then the file.
#
# Inputs. hack/ci-eval-pr.sh exports TF_VAR_gitops_repo, and, because this
# stack declares gitops_token_file, mints a write token narrowed to that
# repository for the unit and exports its file as TF_VAR_gitops_token_file.
# This file gives them to plant.py as KUSTOMIZE_EVAL_GITOPS_REPO and
# KUSTOMIZE_EVAL_GITOPS_TOKEN_FILE, with KUSTOMIZE_EVAL_DIRECTORY.
#
# Isolation class: one file on the GitOps repository's default branch.
# Eligible tier: nightly.

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
  # The settings plant.py reads, the same for the plant and the teardown.
  # Destroy-time provisioners may read only `self`, so the triggers carry them.
  settings = {
    KUSTOMIZE_EVAL_GITOPS_REPO       = var.gitops_repo
    KUSTOMIZE_EVAL_DIRECTORY         = var.cluster_directory
    KUSTOMIZE_EVAL_GITOPS_TOKEN_FILE = var.gitops_token_file
  }
}

resource "null_resource" "kustomize_root" {
  triggers = merge(local.settings, {
    script = "${path.module}/plant.py"
    digest = filesha256("${path.module}/plant.py")
  })

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = local.settings
    command     = <<-EOT
      set -euo pipefail
      python3 "${path.module}/plant.py" plant
    EOT
  }

  # No on_failure = continue: a destroy that cannot close the audit's pull
  # request or remove the planted file fails the run, rather than leaving a
  # Kustomize root that changes obtainability-major-pdb-auto-pr while
  # Terraform records the stack as removed.
  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]
    environment = {
      KUSTOMIZE_EVAL_GITOPS_REPO       = self.triggers.KUSTOMIZE_EVAL_GITOPS_REPO
      KUSTOMIZE_EVAL_DIRECTORY         = self.triggers.KUSTOMIZE_EVAL_DIRECTORY
      KUSTOMIZE_EVAL_GITOPS_TOKEN_FILE = self.triggers.KUSTOMIZE_EVAL_GITOPS_TOKEN_FILE
    }
    command = <<-EOT
      set -euo pipefail
      python3 "${self.triggers.script}" teardown
    EOT
  }
}

# devops-bench reads these two outputs after up() and points the ambient
# kubeconfig at this cluster. The case's checks read the seeded fleet through
# the fixture-role kubeconfigs, not through the ambient one, so the install's
# host cluster is the cluster given here.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
