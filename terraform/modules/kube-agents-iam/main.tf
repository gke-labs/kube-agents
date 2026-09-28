resource "google_service_account" "agent" {
  project      = var.project_id
  account_id   = var.service_account_id
  display_name = var.display_name

  # The scope's bindings are local.scope_role_allowlist intersected with
  # project_roles (scope.tf). A project_roles that carries neither of the two
  # roles able to list and get clusters -- `custom` with an admin-only or a
  # custom-IAM-role list, or [] -- would leave every scoped project declared
  # in the CR and unable to be listed or have a profile created, while
  # Terraform said nothing. Held here because the scope binding's for_each is
  # empty in exactly that case and cannot carry the precondition itself.
  lifecycle {
    precondition {
      condition     = length(local.scope_projects) == 0 || local.scope_can_manage
      error_message = "scope.projects names projects but project_roles (PLATFORM_AGENT_CUSTOM_ROLES on the installer path) carries neither roles/container.clusterViewer nor roles/container.viewer, the two roles that list and get clusters; a custom IAM role is not carried into scoped projects. Add one of the two (it is bound in the host project as well) or empty scope.projects."
    }
  }
}

resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.agent.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${var.ksa_name}]"
}

locals {
  # The list that actually reaches google_project_iam_member.agent_roles.
  #
  # There is deliberately only one, and it is `var.project_roles`. An earlier
  # draft kept a second copy here as the module's "real" default; because the
  # variable is nullable = false with a default of its own, `var.project_roles`
  # is never null and that copy was unreachable -- a role list a reader would
  # take for the granted set while nothing bound it.
  #
  # This local exists as the seam the scoped_clusters coupling goes back into.
  # It was going to read:
  #
  #   length(var.scoped_clusters) > 0
  #   ? [for role in var.project_roles : role if role != "roles/container.viewer"]
  #   : var.project_roles
  #
  # so populating scoped_clusters stripped container.viewer from the agent and
  # relied on the pool to carry it per cluster. roles/container.viewer is what
  # lets an identity read Kubernetes objects in every cluster in the project;
  # without it the agent keeps roles/container.clusterViewer, which reaches the
  # Container API control plane -- listing clusters, `get-credentials` -- and
  # nothing inside a cluster.
  #
  # That residual matters more than it looks. The metadata server is reachable
  # from the agent container in a default install, so the agent can mint a token
  # for this identity whenever it likes, entirely outside the broker. Shrinking
  # what that token is worth is the only control that survives the bypass.
  #
  # SUSPENDED 2026-08-12. The pool carries nothing now: the IAM Condition
  # scoping its members grants nothing for Kubernetes object operations, so the
  # grant was removed outright. See scoped_pool.tf.
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
  agent_project_roles = var.project_roles
}

resource "google_project_iam_member" "agent_roles" {
  #checkov:skip=CKV_GCP_41:Platform agent requires serviceAccountUser role to manage agent workload identities
  #checkov:skip=CKV_GCP_42:Service account is granted non-admin project roles
  #checkov:skip=CKV_GCP_46:Dedicated custom service account used for agent workload identity
  #checkov:skip=CKV_GCP_49:Platform agent requires serviceAccountUser role to manage agent workload identities
  #checkov:skip=CKV_GCP_117:Standard GCP viewer roles granted for read-only telemetry and cluster observability
  for_each = toset(local.agent_project_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.agent.email}"
}
