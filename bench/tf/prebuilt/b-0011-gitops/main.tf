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

# b-0011 through the GitOps fix cycle (Option C pilot, gke-labs/kube-agents#1307).
#
# The devops-bench b-0011 stack applies its manifests and then mutates the
# payments/checkout Deployment in place. This stack seeds the same broken state
# a different way: the broken manifests live in a GitOps repository, this stack
# cuts a per-run branch from a pinned "broken base" commit, installs Argo CD on
# a fresh cluster, and points one Application at that branch. The cluster is
# broken because the repo says so. The agent is read-only on the cluster and
# fixes it by opening a pull request against the run branch; a workflow in the
# repo merges it when it passes, Argo syncs it, and the task's unchanged
# verification_spec grades the result.
#
# manifests/ here is the HEALTHY baseline copied from the b-0011 stack. It is
# not applied at run time. scripts/render-broken-base.sh derives the repo's
# broken base from it, so the repo content and this stack cannot drift apart
# without a diff showing it.
#
# Lifecycle. The run branch is a null_resource with a create and a destroy
# provisioner, so it lives exactly as long as the task cluster: devops-bench's
# teardown (`tofu destroy`) removes both. Reruns are safe because create
# force-resets an existing run branch to the broken base. The default branch's
# content is never written; the pilot-only default-branch mode
# (gitops_switch_default_branch) moves the default-branch pointer to the run
# branch for the run and back on destroy.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
    kind = {
      source  = "tehcyx/kind"
      version = ">= 0.5.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0.0"
    }
  }
}

locals {
  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
  # The task prompt names this branch via {{CLUSTER_NAME}}, so the default
  # must stay in step with bench/tasks/b-0011-gitops/task.yaml.
  run_branch = var.gitops_run_branch != "" ? var.gitops_run_branch : "run/${var.cluster_name}/b-0011"
}

provider "google" {
  project        = var.project_id != "" ? var.project_id : null
  region         = var.location != "" && var.location != "local" ? var.location : null
  default_labels = local.ci_labels
}

provider "kind" {}

module "cluster" {
  source          = "../../modules/cluster"
  infra_provider  = var.infra_provider
  cluster_name    = var.cluster_name
  location        = var.location
  node_count      = var.node_count
  machine_type    = var.machine_type
  project_id      = var.project_id
  kubeconfig_path = var.kubeconfig_path
}

# Per-run branch in the GitOps repo. Destroy-time provisioners may only read
# self.triggers, so every input the delete needs is a trigger.
resource "null_resource" "run_branch" {
  triggers = {
    repo            = var.gitops_repo
    branch          = local.run_branch
    base_sha        = var.gitops_broken_base_sha
    token_file      = var.gitops_token_file
    switch_default  = tostring(var.gitops_switch_default_branch)
    restore_default = var.gitops_restore_default_branch
    script          = "${path.module}/scripts/run-branch.sh"
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "${self.triggers.script} create"
    environment = {
      GITOPS_REPO                   = self.triggers.repo
      GITOPS_RUN_BRANCH             = self.triggers.branch
      GITOPS_BASE_SHA               = self.triggers.base_sha
      GITOPS_TOKEN_FILE             = self.triggers.token_file
      GITOPS_SWITCH_DEFAULT_BRANCH  = self.triggers.switch_default
      GITOPS_RESTORE_DEFAULT_BRANCH = self.triggers.restore_default
    }
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]
    command     = "${self.triggers.script} delete"
    environment = {
      GITOPS_REPO                   = self.triggers.repo
      GITOPS_RUN_BRANCH             = self.triggers.branch
      GITOPS_TOKEN_FILE             = self.triggers.token_file
      GITOPS_SWITCH_DEFAULT_BRANCH  = self.triggers.switch_default
      GITOPS_RESTORE_DEFAULT_BRANCH = self.triggers.restore_default
    }
  }
}

# Install Argo CD core, point an Application at the run branch, and assert the
# seeded condition holds before the agent starts. Runs during `tofu apply`.
resource "null_resource" "setup" {
  depends_on = [module.cluster, null_resource.run_branch]

  triggers = {
    cluster = module.cluster.cluster_name
    branch  = local.run_branch
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "${path.module}/scripts/setup.sh"
    environment = {
      INFRA_PROVIDER    = var.infra_provider
      PROJECT_ID        = var.project_id
      CLUSTER_NAME      = module.cluster.cluster_name
      LOCATION          = var.location
      KUBECONFIG        = pathexpand(var.kubeconfig_path)
      WAIT_TIMEOUT      = var.wait_timeout
      GITOPS_REPO       = var.gitops_repo
      GITOPS_RUN_BRANCH = local.run_branch
      GITOPS_TASK_PATH  = var.gitops_task_path
      GITOPS_TOKEN_FILE = var.gitops_token_file
      ARGOCD_VERSION    = var.argocd_version
      AGENT_HOST_CONTEXT = var.agent_host_context
      AGENT_NAMESPACE    = var.agent_namespace
    }
  }
}

# devops-bench reads these after up() and hands them to the provider's
# ensure_cluster_credentials; omitting them raises ConfigError.
output "cluster_name" {
  value = module.cluster.cluster_name
}

output "cluster_location" {
  value = module.cluster.location
}

output "gitops_run_branch" {
  value = local.run_branch
}
