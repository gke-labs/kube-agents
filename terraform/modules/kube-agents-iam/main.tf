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
      condition     = !local.scope_declares_anything || local.scope_can_manage
      error_message = "scope.projects, scope.folders, scope.organizations, scope.shared_vpc_hosts or scope.metrics_scopes names something but project_roles (PLATFORM_AGENT_CUSTOM_ROLES on the installer path) carries neither roles/container.clusterViewer nor roles/container.viewer, the two roles that list and get clusters; a custom IAM role is not carried into scoped projects. Add one of the two (it is bound in the host project as well) or empty the scope lists."
    }
    # The same shape for a Shared VPC host: the reconcile finds its service
    # projects with compute.projects.get in the host project, which only
    # roles/compute.viewer carries among the allowlist, so a host declared
    # under a role set without it would be resolved here at plan time and read
    # `denied` in the agent's snapshot on every tick.
    # No carve-out for a host that is project_id: it is read under
    # project_roles, but a custom set that carries the permission through a
    # custom role and one that carries it through nothing look the same from
    # here, and the second would freeze the selector every tick; adding
    # roles/compute.viewer costs the first nothing.
    precondition {
      condition     = length(local.scope_shared_vpc_hosts) == 0 || contains(local.scope_roles, local.scope_shared_vpc_lookup_role)
      error_message = "scope.shared_vpc_hosts names a host but project_roles (PLATFORM_AGENT_CUSTOM_ROLES on the installer path) carries no roles/compute.viewer, the role whose compute.projects.get the reconcile needs in the host project to list its service projects; a custom IAM role is not carried into scoped projects. Add it (it is bound in the host project as well) or empty scope.shared_vpc_hosts."
    }
    # A selector the resolver did not resolve: the composition hands the
    # kube-agents-scope-resolver module's members in; a caller that skipped
    # it would otherwise get the host bound and every member unbound, which
    # the reconcile reports as denied on each of them.
    precondition {
      condition     = local.scope_selectors_resolved
      error_message = "scope.shared_vpc_hosts or scope.metrics_scopes names a selector that scope_selector_members has no entry for. Resolve the selectors with the kube-agents-scope-resolver module (terraform/modules/kube-agents-scope-resolver) and pass its members output as scope_selector_members, as terraform/examples/full-install does."
    }
    # The whole resolved set, as far as a plan can count it (scope.tf,
    # scope_listed_projects): the reconcile lists at most the cap and reads
    # the rest over-cap, so the members past it would be bound for nothing.
    # The resolver's own bound is per selector; this is the sum. Held only
    # while a selector is declared: without one the count is the CRD's own
    # list cap plus the management project, a declaration the plan admitted
    # before the selectors existed, and a plan that declares no selector
    # changes nothing about it.
    precondition {
      condition     = length(local.scope_selector_names) == 0 || length(local.scope_listed_projects) <= local.scope_resolved_set_cap
      error_message = "The management project, scope.projects and the projects scope.shared_vpc_hosts and scope.metrics_scopes resolve to come to ${length(local.scope_listed_projects)} once each, past the reconcile's resolved-set cap of ${local.scope_resolved_set_cap} (RESOLVED_SET_CAP in cluster_agent_reconcile.py): the reconcile lists the first ${local.scope_resolved_set_cap} of them, in that order, and reads the rest over-cap with nothing created under them, so their read roles would be reach the agent never uses. Declare fewer projects, a narrower selector, or a folder that holds them (a container's members are listed after these and bound on the container, not one by one). An exclude.projects entry lowers this count only when it names a project exactly: by ID for an entry in scope.projects or any selector member, and by number for a monitored project the selector alone reaches, which the resolver leaves out before naming it. A project both in scope.projects and monitored by a declared Metrics Scope that is excluded by its number alone is dropped by the reconcile but counted here, because the plan does not name a number the exclusion keeps it from reading; drop it from scope.projects, which the exclusion makes redundant. A glob is applied by the reconcile alone."
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
