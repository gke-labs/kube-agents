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

# The scenario driver for bench/tasks/remediation-context-repository-owns-the-fix.
#
# It plants, for the run only, the three parts of the case's fixture
# (fixture/README.md) on the install the run grades: the payments
# HelmRelease directory under clusters/<host cluster>/demo-shop/payments/ in
# the leased GitOps repository, the rendered payments-api workload in the
# demo-shop namespace of the agent host, and the registration of the public
# platform charts repository, with its ref, in the context_repos key of the
# install's GitOps state ConfigMap. plant.py has the steps and the reasons;
# this file only runs it.
#
# The parts are there only while this stack is applied. The case is
# `writes_alone`, so no other unit runs while they are; the one standing
# piece, the public chart repository, is read with no credential and needs
# no change to any pool install.
#
# Teardown on both paths. A failed plant removes what it wrote before it
# exits, because Terraform taints a resource whose create provisioner failed
# and then skips its destroy provisioner. The destroy provisioner removes the
# rest on the success path: the registration, the namespace, then the pull
# requests the agent opened under the directory (labelled
# `audit:stale-closed` before the close) and the four files.
#
# Inputs. Because this stack declares bench_gitops_write_token_file, the
# marker of a writing stack, hack/ci-eval-pr.sh mints the unit a write token
# narrowed to the leased repository, exports its file as
# TF_VAR_bench_gitops_write_token_file, and exports TF_VAR_gitops_repo
# (owner/name), for this unit only. The host cluster and the install's
# namespace arrive as TF_VAR_host_cluster_* and TF_VAR_agent_namespace, the
# project from the deployer. This file gives them to plant.py as the
# CONTEXT_FIX_* settings.
#
# Isolation class: four files on the GitOps repository's default branch, one
# namespace on the agent host, one entry in the install's state ConfigMap.
# Eligible tier: nightly, writers part.

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
  # The case's fixture tree, beside this stack in the repository.
  fixture_dir = abspath("${path.module}/../../../tasks/remediation-context-repository-owns-the-fix/fixture")

  # The settings plant.py reads, the same for the plant and the teardown.
  # Destroy-time provisioners may read only `self`, so the triggers carry them.
  settings = {
    CONTEXT_FIX_GITOPS_REPO           = var.gitops_repo
    CONTEXT_FIX_GITOPS_TOKEN_FILE     = var.bench_gitops_write_token_file
    CONTEXT_FIX_HOST_CLUSTER          = var.host_cluster_name
    CONTEXT_FIX_HOST_CLUSTER_LOCATION = var.host_cluster_location
    CONTEXT_FIX_PROJECT               = var.project_id
    CONTEXT_FIX_AGENT_NAMESPACE       = var.agent_namespace
    CONTEXT_FIX_AGENT_NAME            = var.agent_name
    CONTEXT_FIX_FIXTURE_DIR           = local.fixture_dir
    CONTEXT_FIX_CONTEXT_OWNER         = var.context_repository_owner
    CONTEXT_FIX_CONTEXT_NAME          = var.context_repository_name
    CONTEXT_FIX_CONTEXT_REF           = var.context_repository_ref
    CONTEXT_FIX_BUILD_ID              = var.prow_build_id
    CONTEXT_FIX_PULL_NUMBER           = var.prow_pull_number
  }
}

resource "null_resource" "fixture" {
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

  # No on_failure = continue: a teardown that cannot remove a part exits
  # non-zero, so the destroy reports the failure. devops-bench logs a
  # teardown error and goes on, and the state is the run's own, so nothing
  # applies this destroy again. A file a failed or killed teardown leaves on
  # the default branch is removed by the repository reset
  # (hack/ci_reset_agent_pulls.py, PLANTED_STACK_FILES) at the next lease and
  # before each unit of a case that requests a pull request; a leftover
  # namespace or registration is cleared by the next plant.
  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]
    environment = {
      CONTEXT_FIX_GITOPS_REPO           = self.triggers.CONTEXT_FIX_GITOPS_REPO
      CONTEXT_FIX_GITOPS_TOKEN_FILE     = self.triggers.CONTEXT_FIX_GITOPS_TOKEN_FILE
      CONTEXT_FIX_HOST_CLUSTER          = self.triggers.CONTEXT_FIX_HOST_CLUSTER
      CONTEXT_FIX_HOST_CLUSTER_LOCATION = self.triggers.CONTEXT_FIX_HOST_CLUSTER_LOCATION
      CONTEXT_FIX_PROJECT               = self.triggers.CONTEXT_FIX_PROJECT
      CONTEXT_FIX_AGENT_NAMESPACE       = self.triggers.CONTEXT_FIX_AGENT_NAMESPACE
      CONTEXT_FIX_AGENT_NAME            = self.triggers.CONTEXT_FIX_AGENT_NAME
      CONTEXT_FIX_FIXTURE_DIR           = self.triggers.CONTEXT_FIX_FIXTURE_DIR
      CONTEXT_FIX_CONTEXT_OWNER         = self.triggers.CONTEXT_FIX_CONTEXT_OWNER
      CONTEXT_FIX_CONTEXT_NAME          = self.triggers.CONTEXT_FIX_CONTEXT_NAME
      CONTEXT_FIX_CONTEXT_REF           = self.triggers.CONTEXT_FIX_CONTEXT_REF
      CONTEXT_FIX_BUILD_ID              = self.triggers.CONTEXT_FIX_BUILD_ID
      CONTEXT_FIX_PULL_NUMBER           = self.triggers.CONTEXT_FIX_PULL_NUMBER
    }
    command = <<-EOT
      set -euo pipefail
      python3 "${self.triggers.script}" teardown
    EOT
  }
}

# devops-bench reads these two outputs after up() and points the ambient
# kubeconfig at this cluster, which is the cluster the case names ("this
# install's own cluster") and the one its checks read.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
