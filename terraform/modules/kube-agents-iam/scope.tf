# Grants for the projects, folders, organisations, Shared VPC hosts and Metrics
# Scopes `spec.scope` declares beyond the host project.
#
# The scope is the set of GCP projects whose GKE clusters the Cluster Agent
# reconcile enumerates (docs/designs/multi-project-scope.md §3). The reconcile
# reads the declaration from the PlatformAgent CR; this file is the IAM half of
# the same value, so a project named in `scope.projects`, a folder or
# organisation named in `scope.folders` or `scope.organizations`, and every
# project a `scope.shared_vpc_hosts` or `scope.metrics_scopes` entry resolves
# to, is bound before the CR that declares it is written (the composition
# orders the release after this module; ordering, not IAM propagation, which
# the reconcile's first tick may still run ahead of). `exclude` travels in the
# variable because the composition renders the CR from the same object, but it
# binds nothing and revokes nothing: an exclusion is applied by the reconcile
# after resolution, a glob cannot be evaluated here, and a project named in
# `projects` is bound even when an exclude entry removes it from the resolved
# set -- drop it from `projects` instead. The one exception is a project a
# selector resolved to, which has no list to be dropped from: an exclude entry
# that names a Shared VPC service project by ID, or a monitored project by the
# number the Monitoring API returns (applied in the resolver, before naming),
# keeps it out of the bindings, so the operator's only lever over a selector's
# members also withholds the grant. A monitored project excluded by ID keeps
# its grant: the reconcile names every monitored project with the agent's own
# credentials before it can match an ID entry, and without a grant the naming
# fails, the member is reported by number as unnamed, and the scope prune is
# held on every tick.
#
# What a scoped project gets is `local.scope_roles`, never `var.project_roles`
# (design §6). The allowlist below is the read subset of the default project
# role list, intersected with what the caller granted the host project, so a
# `custom` list that carries roles/container.admin at home does not carry
# container.clusters.impersonate into every other project, and a
# quota-consuming role does not consume quota where the agent only reads.
# Widening what the scope carries is an edit to this list, on purpose.
#
# A container (folder or organisation) carries the same allowlist plus
# `roles/cloudasset.viewer`, because the reconcile resolves a container's
# members with one Cloud Asset Inventory search scoped to it (design §4), and
# a container-level binding is inherited by every project beneath, including
# one created tomorrow: that inheritance is what makes onboarding under a
# declared folder zero-touch, and it is also why an organisation is wide (§9).
#
# A Shared VPC host or a Metrics Scope is not a container: IAM cannot be
# granted on a VPC or on a Metrics Scope, so nothing is inherited through
# either and every project they reach needs its own binding (design §6). The
# bindings are Terraform's and Terraform cannot read the runtime snapshot, so
# the selectors are resolved at plan time, with the same three reads the
# reconcile makes each tick (§10 step 3), by the kube-agents-scope-resolver
# module, whose members this module binds; the section below says why the
# resolution is not made here.

locals {
  # The read roles a scoped project may carry. tests/test_scope_iam.py holds
  # every entry to the module's default project_roles, so the allowlist cannot
  # name a role the agent does not hold at home, and holds the host-only
  # roles out: roles/cloudtrace.user is one of those, since Google's Trace
  # User role carries create, delete and update on Trace-console objects
  # beside the two trace reads, and the observability helpers that need the
  # reads look at the host project's own traces (design §6).
  scope_role_allowlist = [
    "roles/container.clusterViewer",
    "roles/container.viewer",
    "roles/compute.viewer",
    "roles/monitoring.viewer",
    "roles/logging.viewer",
    "roles/iam.securityReviewer",
  ]

  # The allowlist entries that carry both container.clusters.list and
  # container.clusters.get, which is what the reconcile needs: list to
  # discover, get for each cluster's credentials and liveness probe.
  # roles/iam.securityReviewer lists but cannot get, so a project bound with it
  # alone reads `ok` and then fails every profile create; the precondition on
  # google_service_account.agent (main.tf) refuses a plan whose intersection
  # carries neither of these two.
  scope_managing_roles = [
    "roles/container.clusterViewer",
    "roles/container.viewer",
  ]

  scope_roles = [for role in local.scope_role_allowlist : role if contains(var.project_roles, role)]

  scope_can_manage = anytrue([for role in local.scope_roles : contains(local.scope_managing_roles, role)])

  # The host project is always in scope and already carries project_roles;
  # naming it in scope.projects is harmless and binds nothing twice.
  scope_projects = toset([for project in var.scope.projects : project if project != var.project_id])

  # Every project bound with scope_roles: the explicit ones and the ones the
  # two selectors resolved to (below), once each, so a project both an entry
  # and a selector name is one binding with one state address. A Shared VPC
  # host that is not otherwise in scope is bound too, with the lookup role
  # alone (below), in the same resource.
  scope_bound_projects = setunion(local.scope_projects, local.scope_selector_projects)

  scope_bindings = merge(
    {
      for pair in setproduct(sort(tolist(local.scope_bound_projects)), local.scope_roles) :
      "${pair[0]}/${pair[1]}" => { project = pair[0], role = pair[1] }
    },
    {
      for host in sort(tolist(local.scope_lookup_only_hosts)) :
      "${host}/${local.scope_shared_vpc_lookup_role}" => { project = host, role = local.scope_shared_vpc_lookup_role }
    },
  )

  # The one role a container carries beyond the allowlist: the reconcile's
  # `asset search-all-resources --scope=<container>` needs it on the container
  # it searches, and nowhere else. Not intersected with project_roles, because
  # the host project never holds it (the host is listed with `clusters list`).
  scope_container_asset_role = "roles/cloudasset.viewer"

  scope_container_roles = concat(local.scope_roles, [local.scope_container_asset_role])

  scope_folders       = toset(var.scope.folders)
  scope_organizations = toset(var.scope.organizations)

  scope_folder_bindings = {
    for pair in setproduct(sort(tolist(local.scope_folders)), local.scope_container_roles) :
    "${pair[0]}/${pair[1]}" => { folder = pair[0], role = pair[1] }
  }

  scope_organization_bindings = {
    for pair in setproduct(sort(tolist(local.scope_organizations)), local.scope_container_roles) :
    "${pair[0]}/${pair[1]}" => { organization = pair[0], role = pair[1] }
  }

  # What the manageability precondition in main.tf counts: any declaration
  # that binds outside the host project.
  scope_declares_anything = length(local.scope_projects) + length(local.scope_folders) + length(local.scope_organizations) + length(local.scope_shared_vpc_hosts) + length(local.scope_metrics_scopes) > 0
}

# ─── The two selectors, resolved before this module runs ──────────────────────
#
# A Shared VPC host or a Metrics Scope is not a container: IAM cannot be
# granted on a VPC or on a Metrics Scope, so nothing is inherited through
# either and every project they reach needs its own binding (design §6). The
# resolution to projects happens at plan time in the kube-agents-scope-resolver
# module, which the composition calls beside this one and whose `members`
# output is this module's `scope_selector_members` input. Not here, because
# the composition calls this module with a module-level depends_on (the
# Workload Identity pool has to exist before its binding), which defers every
# data source in the module to apply time whenever a target has a planned
# change, and a for_each keyed on a deferred read fails the plan. The
# precondition in main.tf refuses a declared selector that has no entry in
# the input, so a caller that skips the resolver is told so rather than
# getting the host bound and its members not. What the selectors do not have
# is the zero-touch onboarding a container gets: a service project attached,
# or a project added to the scope, after the last apply reads `denied` in
# the snapshot until the next `upgrade.sh` binds it.

locals {
  scope_shared_vpc_hosts = toset(var.scope.shared_vpc_hosts)
  scope_metrics_scopes   = toset(var.scope.metrics_scopes)

  # The snapshot names the resolver's output uses, one per declared selector;
  # the precondition in main.tf requires each in scope_selector_members.
  scope_selector_names = concat(
    [for host in local.scope_shared_vpc_hosts : "sharedVpcHosts/${host}"],
    [for scope in local.scope_metrics_scopes : "metricsScopes/${scope}"],
  )
  scope_selectors_resolved = alltrue([for name in local.scope_selector_names : contains(keys(var.scope_selector_members), name)])

  # The role whose compute.projects.get the reconcile's host lookup needs in
  # the host project; the precondition in main.tf refuses a host without it.
  scope_shared_vpc_lookup_role = "roles/compute.viewer"

  # What the selectors add to the set that carries the allowlist: their
  # members, less an exact exclude entry and the host project (which carries
  # project_roles already), plus each Metrics Scope's scoping project, which
  # the reconcile's lookup has to read (resourcemanager.projects.get and
  # .list there, which both managing roles carry, so the manageability
  # precondition in main.tf covers the lookup too) whether or not an exclude
  # entry names it: the exclusion drops its clusters from the set, not the
  # lookup that resolves the selector, which would otherwise read `denied`
  # every tick and freeze the selector. Only the declared selectors' entries
  # are read, so a stale key in the input binds nothing.
  # An exact exclude entry withholds a Shared VPC service project's grant
  # only: a monitored project excluded by ID still needs the grant for the
  # reconcile's naming call (the header comment says why), and one excluded
  # by number never reached this input.
  scope_selector_projects = toset(concat(
    [
      for pair in flatten([
        for name in local.scope_selector_names : [
          for project in lookup(var.scope_selector_members, name, []) : { name = name, project = project }
        ]
      ]) : pair.project
      if pair.project != var.project_id && !(startswith(pair.name, "sharedVpcHosts/") && contains(var.scope.exclude.projects, pair.project))
    ],
    [for scope in local.scope_metrics_scopes : scope if scope != var.project_id],
  ))

  # What the reconcile lists of the resolved set: at most the declared cap
  # (spec.scope.maxProjects, scope.max_projects here, the reconcile's
  # RESOLVED_SET_CAP as its default; design §3), the management project
  # included, in a fixed order -- the management project, scope.projects, the
  # selectors' members, then the containers' -- and a project past the cap
  # reads `over-cap` with nothing created under it. The first three groups are
  # known here at plan time, so a declaration they alone carry past the cap is
  # refused (main.tf) rather than bound, while a selector is declared: the
  # read roles in the members past it would be reach the agent never uses.
  # Without a selector and at the default cap the count is scope.projects and the
  # management project, which the CRD's own list cap bounds and this module admitted
  # before the selectors existed, so such a plan is not refused for it and the
  # reconcile reads a hundred-and-first over-cap as it did; a cap declared below the
  # default is the operator's own number, and the precondition holds against it
  # whether or not a selector is declared. Counted as the
  # reconcile counts, once
  # each and less an exact exclude entry: by ID on both legs (a monitored
  # project excluded by ID keeps its grant but leaves the set, so it leaves the
  # count), and by number on the selector leg as well, where the resolver has
  # already left an excluded number out without naming it. A project both in
  # scope.projects and monitored by a declared Metrics Scope that is excluded
  # by its number alone is therefore
  # counted here although the reconcile, which names it with the explicit
  # grant, drops it: the plan cannot tie a number to an entry without the read
  # the exclusion exists to avoid, so the count is a bound, over by exactly
  # those projects, and the error names the remedy (drop the entry from
  # scope.projects, which the exclusion makes redundant). A glob is the
  # reconcile's alone, so a set only a glob brings under the cap at runtime is
  # refused here and wants its entries named exactly. Containers are not counted: they come
  # last in the order and their binding is one on the container rather than
  # one per member, so a member the resolver listed for the scoped service
  # account pool (scope_container_members, scoped_pool.tf) joins the pool
  # and nothing here.
  scope_resolved_set_cap = var.scope.max_projects
  # The cap's default (the reconcile's RESOLVED_SET_CAP, the variable's default): the one
  # declaration the whole-set precondition admitted before the cap was declarable, the
  # CRD's hundred explicit projects with no selector, is admitted at this value alone;
  # a cap declared below it holds the precondition with or without a selector.
  scope_default_cap = 100

  scope_listed_projects = setunion(
    toset([var.project_id]),
    toset([for project in var.scope.projects : project if !contains(var.scope.exclude.projects, project)]),
    toset([
      for project in flatten([for name in local.scope_selector_names : lookup(var.scope_selector_members, name, [])]) : project
      if !contains(var.scope.exclude.projects, project)
    ]),
  )

  # A Shared VPC host the reconcile's lookup has to read (compute.projects.get)
  # but that is not otherwise in scope: it is not among its own service
  # projects, and is named in scope.projects when its clusters are wanted, so
  # the rest of the allowlist would have no consumer there. It gets the lookup
  # role alone, whether or not an exclude entry names it, for the reason the
  # scoping project is bound: an unreadable host freezes its selector.
  scope_lookup_only_hosts = toset([
    for host in local.scope_shared_vpc_hosts : host
    if host != var.project_id && !contains(local.scope_bound_projects, host)
  ])
}

resource "google_project_iam_member" "scope_roles" {
  #checkov:skip=CKV_GCP_41:The scope binds read roles only, filtered through local.scope_role_allowlist
  #checkov:skip=CKV_GCP_42:Service account is granted non-admin project roles
  #checkov:skip=CKV_GCP_46:Dedicated custom service account used for agent workload identity
  #checkov:skip=CKV_GCP_49:The scope binds read roles only, filtered through local.scope_role_allowlist
  #checkov:skip=CKV_GCP_117:Standard GCP viewer roles granted for read-only cluster discovery in scoped projects
  for_each = local.scope_bindings

  project = each.value.project
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.agent.email}"
}

resource "google_folder_iam_member" "scope_roles" {
  #checkov:skip=CKV_GCP_41:The scope binds read roles only, filtered through local.scope_role_allowlist, plus roles/cloudasset.viewer
  #checkov:skip=CKV_GCP_42:Service account is granted non-admin folder roles
  #checkov:skip=CKV_GCP_46:Dedicated custom service account used for agent workload identity
  #checkov:skip=CKV_GCP_49:The scope binds read roles only, filtered through local.scope_role_allowlist, plus roles/cloudasset.viewer
  #checkov:skip=CKV_GCP_117:Standard GCP viewer roles granted for read-only cluster discovery beneath a declared folder
  for_each = local.scope_folder_bindings

  folder = "folders/${each.value.folder}"
  role   = each.value.role
  member = "serviceAccount:${google_service_account.agent.email}"
}

resource "google_organization_iam_member" "scope_roles" {
  #checkov:skip=CKV_GCP_41:The scope binds read roles only, filtered through local.scope_role_allowlist, plus roles/cloudasset.viewer
  #checkov:skip=CKV_GCP_42:Service account is granted non-admin organisation roles
  #checkov:skip=CKV_GCP_46:Dedicated custom service account used for agent workload identity
  #checkov:skip=CKV_GCP_49:The scope binds read roles only, filtered through local.scope_role_allowlist, plus roles/cloudasset.viewer
  #checkov:skip=CKV_GCP_117:Standard GCP viewer roles granted for read-only cluster discovery across a declared organisation
  for_each = local.scope_organization_bindings

  org_id = each.value.organization
  role   = each.value.role
  member = "serviceAccount:${google_service_account.agent.email}"
}
