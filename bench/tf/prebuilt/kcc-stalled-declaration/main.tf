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

# The scenario driver for
# bench/tasks/fleet-audit-manifest-fix-names-stalled-config-connector.
#
# It plants, for the run only, a Config Connector object that stays
# `Ready=False`, and the declaration of that object in the GitOps repository,
# where the stockout audit's fix for the seeded `pinned-inference-pool` edits
# it. plant.py has the steps and the reasons; this file only runs it.
#
# The object is a `ContainerNodePool` for the seeded pool, in a namespace of its
# own on the cluster that runs Config Connector. Its `clusterRef` names a
# `ContainerCluster` that the namespace does not hold, so Config Connector stops
# at `DependencyNotFound` and makes no call to GCP, and the object carries
# `cnrm.cloud.google.com/deletion-policy: abandon`, so its delete cannot delete
# the pool. The real pool is never read or changed.
#
# When Config Connector runs in namespaced mode, the plant also creates a
# `ConfigConnectorContext` in the namespace, with the service account in
# `kcc_google_service_account`, and waits until its controller is healthy. The
# plant stops with an error when that mode has no service account set.
# When the add-on's operator cannot finish the context delete, the teardown
# removes the operator's finalizer from the objects in that namespace, but
# only when no Config Connector object is left there (plant.py, teardown).
#
# Teardown on both paths. A failed plant removes what it wrote before it
# exits, because Terraform taints a resource whose create provisioner failed
# and then skips its destroy provisioner. The destroy provisioner removes the
# rest on the success path: the pull request the run opened against the
# declaration, the declaration, the object and the namespace.
#
# Inputs. Each variable in variables.tf can come from the environment as
# TF_VAR_<name>: TF_VAR_project_id, TF_VAR_kcc_cluster_name,
# TF_VAR_kcc_cluster_location, TF_VAR_gitops_repo (owner/name),
# TF_VAR_bench_gitops_write_token_file and, in namespaced mode,
# TF_VAR_kcc_google_service_account. The harness will give these to a stack
# only when it declares bench_gitops_write_token_file (variables.tf); that
# depends on #4742, which is not merged. The task sets
# kcc_namespace, seeded_slot and seeded_pool. This file gives them to
# plant.py as KCC_EVAL_PROJECT, KCC_EVAL_CLUSTER, KCC_EVAL_LOCATION,
# KCC_EVAL_NAMESPACE, KCC_EVAL_SLOT, KCC_EVAL_POOL, KCC_EVAL_GITOPS_REPO,
# KCC_EVAL_GITOPS_TOKEN_FILE and KCC_EVAL_GOOGLE_SERVICE_ACCOUNT. With no
# token file, plant.py reads the GitHub token from BENCH_GITHUB_TOKEN.
#
# Isolation class: one namespace on the Config Connector cluster, and one file
# on the GitOps repository's default branch. Eligible tier: nightly.

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
  # KCC_EVAL_GOOGLE_SERVICE_ACCOUNT is read only in namespaced mode.
  settings = {
    KCC_EVAL_PROJECT                = var.project_id
    KCC_EVAL_CLUSTER                = var.kcc_cluster_name
    KCC_EVAL_LOCATION               = var.kcc_cluster_location
    KCC_EVAL_NAMESPACE              = var.kcc_namespace
    KCC_EVAL_SLOT                   = var.seeded_slot
    KCC_EVAL_POOL                   = var.seeded_pool
    KCC_EVAL_GITOPS_REPO            = var.gitops_repo
    KCC_EVAL_GITOPS_TOKEN_FILE      = var.bench_gitops_write_token_file
    KCC_EVAL_GOOGLE_SERVICE_ACCOUNT = var.kcc_google_service_account
  }
}

resource "null_resource" "stalled_declaration" {
  triggers = merge(local.settings, {
    script = "${path.module}/plant.py"
    digest = filesha256("${path.module}/plant.py")
  })

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = local.settings
    command     = <<-EOT
      set -euo pipefail
      if [ -z "${var.kcc_cluster_name}" ] || [ -z "${var.kcc_cluster_location}" ] || [ -z "${var.gitops_repo}" ]; then
        echo "ERROR: kcc_cluster_name, kcc_cluster_location and gitops_repo are required: the cluster that runs Config Connector, its location, and the GitOps repository the audit writes to." >&2
        exit 1
      fi
      python3 "${path.module}/plant.py" plant
    EOT
  }

  # No on_failure = continue: a destroy that cannot remove the planted file,
  # close the pull requests or delete the namespace fails the run, rather than
  # leaving them behind while Terraform records the stack as removed.
  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]
    environment = {
      KCC_EVAL_PROJECT           = self.triggers.KCC_EVAL_PROJECT
      KCC_EVAL_CLUSTER           = self.triggers.KCC_EVAL_CLUSTER
      KCC_EVAL_LOCATION          = self.triggers.KCC_EVAL_LOCATION
      KCC_EVAL_NAMESPACE         = self.triggers.KCC_EVAL_NAMESPACE
      KCC_EVAL_SLOT              = self.triggers.KCC_EVAL_SLOT
      KCC_EVAL_POOL              = self.triggers.KCC_EVAL_POOL
      KCC_EVAL_GITOPS_REPO       = self.triggers.KCC_EVAL_GITOPS_REPO
      KCC_EVAL_GITOPS_TOKEN_FILE = self.triggers.KCC_EVAL_GITOPS_TOKEN_FILE
    }
    command = <<-EOT
      set -euo pipefail
      python3 "${self.triggers.script}" teardown
    EOT
  }
}

# devops-bench reads these two outputs after up() and points the ambient
# kubeconfig at this cluster, which is the cluster the case's safeguard on the
# planted object reads.
output "cluster_name" {
  value = var.kcc_cluster_name
}

output "cluster_location" {
  value = var.kcc_cluster_location
}
