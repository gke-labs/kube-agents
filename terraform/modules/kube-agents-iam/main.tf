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
  agent_project_roles = (
    var.project_roles != null ? var.project_roles :
    length(var.scoped_clusters) > 0
    ? [for role in local.agent_read_only_roles : role if role != "roles/container.viewer"]
    : local.agent_read_only_roles
  )
}

resource "google_project_iam_member" "agent_roles" {
  for_each = toset(local.agent_project_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.agent.email}"
}
