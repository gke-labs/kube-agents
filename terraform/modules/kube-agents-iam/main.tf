resource "google_service_account" "agent" {
  project      = var.project_id
  account_id   = var.service_account_id
  display_name = var.display_name
}

resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.agent.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${var.ksa_name}]"
}

locals {
  # Mirrors the `read-only` set in k8s-operator/scripts/provision_04_gcp_iam.sh.
  # `tests/test_agent_iam_ceiling.py` runs the real bash and compares the two,
  # so the mirror is checked rather than merely intended.
  agent_read_only_roles = [
    "roles/container.clusterViewer",
    "roles/container.viewer",
    "roles/compute.viewer",
    "roles/monitoring.viewer",
    "roles/logging.viewer",
    "roles/iam.serviceAccountUser",
    "roles/iam.securityReviewer",
    "roles/mcp.toolUser",
  ]

  # roles/container.viewer is what lets an identity read Kubernetes objects in
  # every cluster in the project. Once the pool carries it per-cluster, the
  # agent's own identity keeps only container.clusterViewer, which reaches the
  # Container API control plane -- listing clusters, `get-credentials` -- and
  # nothing inside a cluster.
  #
  # That residual matters more than it looks. The metadata server is reachable
  # from the agent container in a default install, so the agent can mint a token
  # for this identity whenever it likes, entirely outside the broker. Shrinking
  # what that token is worth is the only control that survives the bypass.
  #
  # SUSPENDED 2026-08-12. This read:
  #
  #   length(var.scoped_clusters) > 0
  #   ? [for role in local.agent_read_only_roles : role if role != "roles/container.viewer"]
  #   : local.agent_read_only_roles
  #
  # so populating scoped_clusters stripped container.viewer from the agent and
  # relied on the pool to carry it per cluster. The pool carries nothing now:
  # the IAM Condition scoping its members grants nothing for Kubernetes object
  # operations, so the grant was removed outright. See scoped_pool.tf.
  #
  # Left as it was, this is a total outage rather than a narrowing -- the agent
  # cannot read objects and no pool member can either. The runtime flag does not
  # rescue it. CREDENTIAL_PROXY_SCOPED_SA_POOL=0 falls back to the ambient
  # credential, and the ambient credential is precisely the one this stripped.
  #
  # The reasoning above is still correct and the metadata-server argument is the
  # strongest reason to want it back. Restore it in the same change that lands
  # per-cluster RBAC, gated on the pool granting something, with a test that a
  # read still succeeds afterwards.
  agent_project_roles = (
    var.project_roles != null ? var.project_roles : local.agent_read_only_roles
  )
}

resource "google_project_iam_member" "agent_roles" {
  for_each = toset(local.agent_project_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.agent.email}"
}
