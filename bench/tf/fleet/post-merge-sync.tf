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

# The post-merge fixture's sync (#6905): on a project config-sync.tf turns
# Config Sync on for (post_merge_sync_projects), one RepoSync that syncs the pool
# repository's clusters/<seeded-a>/seeded-post-merge/ into the
# seeded-post-merge namespace (defects-a.tf), the directory the fleet audit
# writes order-ledger's budget into, beside its declaration. The budget is
# merged there at steady state. The case removes it and Config Sync prunes it
# (its default for an object it applied that leaves the source); the agent's
# fix is merged and Config Sync applies it again.
#
# config-sync.tf turns the feature on with no git source, so the only sync on
# the cluster is the RepoSync, and its reconciler can write what the Role
# below allows in that one namespace: PodDisruptionBudgets. It reads the
# repository through a read-only deploy key on that repository alone, whose
# private half scripts/provision_ci_pool_post_merge_sync.sh keeps in the
# project's Secret Manager; the reconciler reads it into the namespace's
# git-creds Secret on each apply. Every block here is counted by
# local.post_merge_sync_enabled, so an unlisted project reads no secret.

locals {
  # Literal, not the namespace resource's name: that resource is counted.
  post_merge_namespace = "seeded-post-merge"
  # scripts/provision_ci_pool_post_merge_sync.sh writes this secret.
  post_merge_deploy_key_secret = "kube-agents-eval-gitops-deploy-key"
  # The pool convention (gitops_repo_for_project in hack/ci-deploy.sh); a dev
  # project names its own with -var post_merge_gitops_repo.
  post_merge_gitops_repo = coalesce(var.post_merge_gitops_repo, "gke-agentic/${var.project_id}-infra")
  post_merge_git_creds   = "git-creds"
  # Config Sync names the reconciler of a RepoSync called repo-sync (the
  # manifest below; fixtures.json probes it) this, in config-management-system.
  post_merge_reconciler_ksa = "ns-reconciler-${local.post_merge_namespace}"
  config_sync_system_ns     = "config-management-system"
  # How often the reconciler polls the branch: the post-merge harness waits on it.
  post_merge_sync_period = "15s"
  post_merge_repo_sync_manifest = {
    apiVersion = "configsync.gke.io/v1beta1"
    kind       = "RepoSync"
    metadata = {
      name      = "repo-sync"
      namespace = local.post_merge_namespace
      labels    = local.fleet_labels
    }
    spec = {
      sourceType = "git"
      git = {
        repo      = "git@github.com:${local.post_merge_gitops_repo}.git"
        branch    = var.post_merge_gitops_branch
        dir       = "clusters/${google_container_cluster.seeded_a.name}/${local.post_merge_namespace}"
        auth      = "ssh"
        period    = local.post_merge_sync_period
        secretRef = { name = local.post_merge_git_creds }
      }
    }
  }
}

# The deploy key's private half and GitHub's SSH host keys, as JSON. Read on
# every plan, and stored in this stack's state, which only the reconciler and
# the project's owners can read.
data "google_secret_manager_secret_version" "post_merge_deploy_key" {
  count = local.post_merge_sync_enabled ? 1 : 0

  secret = local.post_merge_deploy_key_secret
}

resource "kubernetes_secret_v1" "post_merge_git_creds" {
  count = local.post_merge_sync_enabled ? 1 : 0

  metadata {
    name      = local.post_merge_git_creds
    namespace = local.post_merge_namespace
  }
  # known_hosts turns on host key checking, so the reconciler talks to GitHub alone.
  data = {
    ssh         = jsondecode(data.google_secret_manager_secret_version.post_merge_deploy_key[0].secret_data).ssh
    known_hosts = jsondecode(data.google_secret_manager_secret_version.post_merge_deploy_key[0].secret_data).known_hosts
  }
  depends_on = [kubernetes_namespace_v1.seeded_post_merge]
}

# What the reconciler may write here: budgets, the only object the case
# merges. It reads Deployments too, the declaration it leaves alone.
resource "kubernetes_role_v1" "post_merge_sync" {
  count = local.post_merge_sync_enabled ? 1 : 0

  metadata {
    name      = "post-merge-sync"
    namespace = local.post_merge_namespace
  }
  rule {
    api_groups = ["policy"]
    resources  = ["poddisruptionbudgets"]
    verbs      = ["get", "list", "watch", "create", "update", "patch", "delete"]
  }
  rule {
    api_groups = ["apps"]
    resources  = ["deployments"]
    verbs      = ["get", "list", "watch"]
  }
  depends_on = [kubernetes_namespace_v1.seeded_post_merge]
}

resource "kubernetes_role_binding_v1" "post_merge_sync" {
  count = local.post_merge_sync_enabled ? 1 : 0

  metadata {
    name      = "post-merge-sync"
    namespace = local.post_merge_namespace
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.post_merge_sync[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = local.post_merge_reconciler_ksa
    namespace = local.config_sync_system_ns
  }
}

# The RepoSync, applied by repo_sync.py once Config Sync has installed its CRD;
# config-connector.tf's terraform_data has the reasoning. Replaced when the
# script or the object changes, or seeded-a is re-created; the replace is a
# standing entry in reconcile-allow.json.
resource "terraform_data" "post_merge_repo_sync" {
  count = local.post_merge_sync_enabled ? 1 : 0

  triggers_replace = [
    filesha256("${path.module}/repo_sync.py"),
    jsonencode(local.post_merge_repo_sync_manifest),
    google_container_cluster.seeded_a.master_auth[0].cluster_ca_certificate,
  ]

  provisioner "local-exec" {
    interpreter = ["python3"]
    command     = "${path.module}/repo_sync.py"
    environment = {
      K8S_HOST           = "https://${google_container_cluster.seeded_a.endpoint}"
      K8S_CA_BASE64      = google_container_cluster.seeded_a.master_auth[0].cluster_ca_certificate
      REPO_SYNC_MANIFEST = jsonencode(local.post_merge_repo_sync_manifest)
    }
  }

  depends_on = [
    google_gke_hub_feature_membership.seeded_a_config_sync,
    kubernetes_namespace_v1.seeded_post_merge,
    kubernetes_secret_v1.post_merge_git_creds,
    kubernetes_role_binding_v1.post_merge_sync,
  ]
}
