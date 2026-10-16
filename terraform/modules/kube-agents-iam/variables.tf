variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "service_account_id" {
  description = "IAM Service Account ID for Kube-Agents. Fixed per project, so a second install in the same project must set its own. Passing null selects this default (nullable = false), which lets root modules expose a passthrough variable."
  type        = string
  nullable    = false
  default     = "kubeagents-platform-gsa"

  validation {
    condition     = can(regex("^[a-z]([-a-z0-9]{4,28}[a-z0-9])$", var.service_account_id))
    error_message = "service_account_id must be 6-30 characters, start with a lowercase letter, and contain only lowercase letters, digits, and hyphens."
  }
}

variable "display_name" {
  description = "Display name for the service account. Override when the module is instantiated for something other than the platform agent (e.g. the LiteLLM gateway's Vertex AI identity)."
  type        = string
  default     = "Kube-Agents Platform Agent Service Account"
}

variable "namespace" {
  description = "Kubernetes namespace where Kube-Agents runs"
  type        = string
  default     = "kubeagents-system"
}

variable "ksa_name" {
  description = "Kubernetes Service Account name"
  type        = string
  default     = "kubeagents-platform-agent"
}

variable "project_roles" {
  description = <<-EOT
    Project-level IAM roles granted to the agent's service account in the host
    project -- `local.agent_project_roles` in main.tf reads this and nothing
    else -- and the list the scope's per-project bindings (scope.tf) are drawn
    from. The default below mirrors `read_only_roles` in
    terraform/examples/full-install/main.tf, and
    tests/test_scoped_sa_pool_iam.py compares the two, so the mirror is checked
    rather than merely intended. Set [] to grant nothing and manage roles
    elsewhere. The full-install composition resolves permission_set into an
    explicit list before it calls this module, so on that path this variable is
    always named. See the security-and-iam reference for what each role is for.

    The default was going to depend on scoped_pool_enabled: with the pool armed
    the per-project accounts would carry roles/container.viewer and the agent
    would drop it, keeping roles/container.clusterViewer -- enough to enumerate
    the fleet and run `get-credentials`, not enough to read anything inside a
    cluster. That coupling is suspended, because the pool grants nothing (see
    scoped_pool.tf). It has to come back in the same change that gives a pool
    member authority: narrowing the agent while the pool grants nothing is a
    total outage, and arming the pool while the agent stays wide leaves the
    ceiling the pool exists to remove.
  EOT
  type        = list(string)
  nullable    = false
  default = [
    "roles/container.clusterViewer",
    "roles/container.viewer",
    "roles/compute.viewer",
    "roles/monitoring.viewer",
    "roles/logging.viewer",
    "roles/cloudtrace.user",
    "roles/iam.serviceAccountUser",
    "roles/iam.securityReviewer",
    "roles/mcp.toolUser",
    "roles/serviceusage.serviceUsageConsumer",
  ]
}

variable "scoped_pool_enabled" {
  description = <<-EOT
    Arms the scoped service account pool: one reader service account per
    project the plan can list in the scope (the host project, scope.projects
    less an exact exclude.projects entry, each selector's members, and each
    declared folder's and organisation's members, which the resolver lists
    while the pool is armed and hands in through scope_container_members),
    each created in project_id and keyed on the bare project id -- see
    scoped_pool.tf. False, the default, provisions no pool and leaves the
    agent's single wide identity in place, whatever the scope declares:
    arming is a separate, explicit switch so that declaring projects on its
    own arms nothing (docs/designs/multi-project-scope.md §6).

    As of 2026-08-12 these accounts hold no IAM grant. They were scoped by an
    IAM Condition on the cluster's resource.name; that grants nothing for
    Kubernetes object operations, and un-conditioned the same binding is
    project-wide container.viewer. Both are gone. Authority arrives with
    per-cluster RBAC -- see scoped_pool.tf -- and until then the broker runs on
    the ambient credential by default.

    Every project the agent is expected to read inside must be listed when
    the pool is armed. A cluster in one that is not -- created under a
    declared folder or organisation, or added to the scope, since the last
    apply -- is refused by the broker rather than served by a wider
    credential, which is intended, but it means the listed set and the live
    fleet are two things that can drift, and the drift shows up as a refusal
    until the next apply lists it.
  EOT
  type        = bool
  nullable    = false
  default     = false
}

variable "scoped_pool_max_accounts" {
  description = <<-EOT
    The most pool members the plan may create in project_id, a bound the
    operator declares from the service-account quota headroom the project has
    free: the quota (100 per project by GCP's default) is shared with the
    agent's own accounts, the project's default accounts and every other
    tenant, and the module cannot read it. The default is the quota itself,
    so at the default a pool of ninety-odd passes the check and meets the quota
    mid-apply; set this to the headroom, and raise the quota before raising
    it. A pool past the declared bound is refused at plan (main.tf) rather
    than part-way through an apply. Read only while scoped_pool_enabled is
    true.
  EOT
  type        = number
  nullable    = false
  default     = 100

  validation {
    condition     = var.scoped_pool_max_accounts >= 1 && floor(var.scoped_pool_max_accounts) == var.scoped_pool_max_accounts
    error_message = "scoped_pool_max_accounts is a whole number of at least 1: the number of service accounts the pool may create in project_id."
  }
}

variable "scope" {
  description = <<-EOT
    The projects beyond project_id whose GKE clusters the Cluster Agent
    reconcile enumerates, mirroring `spec.scope` on the PlatformAgent CR
    (docs/designs/multi-project-scope.md §3). Each project in `projects` gets
    the read roles in `local.scope_roles` (scope.tf): the module's read
    allowlist intersected with project_roles, never project_roles itself.
    `exclude` travels with the declaration so the composition can render the CR
    from one object; it binds nothing here.

    `folders` and `organizations` are numeric Resource Manager container IDs;
    each gets the same allowlist plus roles/cloudasset.viewer, bound on the
    container itself, so every project beneath it (including one created
    later) inherits the grant and the reconcile can search the container's
    asset index for clusters (design §4, §6). An organisation binding is wide:
    the design recommends folders until the scoped service account pool
    grants authority (§9).

    `shared_vpc_hosts` and `metrics_scopes` are project IDs: a Shared VPC host
    project, whose attached service projects are in scope, and the scoping
    project of a Cloud Monitoring Metrics Scope, whose monitored projects are.
    Neither is a Resource Manager container, so nothing is inherited through
    them: each is resolved to its projects at plan time by the
    kube-agents-scope-resolver module, handed in through
    scope_selector_members, and this module binds the same allowlist in every
    one, the scoping project included and roles/compute.viewer alone in a host
    not otherwise in scope, because the reconcile's lookups read them. A project attached or linked after the
    last apply reads `denied` until the next one. An exclude entry that names
    a Shared VPC service project by ID, or a monitored project by the project
    number the Monitoring API returns, keeps it out of the bindings; a
    monitored project excluded by ID keeps its grant, which the reconcile's
    naming call needs before the exclusion can match; a glob is evaluated by
    the reconcile alone.

    `max_projects` is the resolved-set cap, spec.scope.maxProjects: the most
    projects the reconcile lists per run, the management project included,
    100 by default; a declaration whose explicit projects and selector
    members alone exceed it is refused at plan time (main.tf) while a
    selector is declared or the cap is below its default (the CRD's hundred
    explicit projects with no selector, at the default, was admitted before
    the cap existed and still is), and the chart renders the same value on
    the CR.

    Empty, the default, binds nothing and the reconcile lists project_id alone.
  EOT
  type = object({
    projects         = optional(list(string), [])
    folders          = optional(list(string), [])
    organizations    = optional(list(string), [])
    shared_vpc_hosts = optional(list(string), [])
    metrics_scopes   = optional(list(string), [])
    max_projects     = optional(number, 100)
    exclude = optional(object({
      projects = optional(list(string), [])
      clusters = optional(list(object({
        project_id   = string
        location     = string
        cluster_name = string
      })), [])
    }), {})
  })
  nullable = false
  default  = {}

  # The resolved-set cap the reconcile lists per run, the management project included:
  # spec.scope.maxProjects, with the CRD's bounds. The plan refuses a declaration whose
  # explicit projects and selector members alone exceed it (main.tf), and the per-selector
  # cap in kube-agents-scope-resolver is the same number.
  validation {
    condition     = var.scope.max_projects >= 1 && var.scope.max_projects <= 5000 && floor(var.scope.max_projects) == var.scope.max_projects
    error_message = "scope.max_projects is a whole number from 1 to 5000, the bounds the CRD puts on spec.scope.maxProjects."
  }

  validation {
    condition = (
      length(var.scope.projects) <= 100
      && length(var.scope.folders) <= 100
      && length(var.scope.organizations) <= 100
      && length(var.scope.shared_vpc_hosts) <= 100
      && length(var.scope.metrics_scopes) <= 100
      && length(var.scope.exclude.projects) <= 100
      && length(var.scope.exclude.clusters) <= 100
    )
    error_message = "scope.projects, scope.folders, scope.organizations, scope.shared_vpc_hosts, scope.metrics_scopes, scope.exclude.projects and scope.exclude.clusters each carry at most 100 entries, the cap the CRD enforces on the same lists."
  }

  validation {
    condition = alltrue([
      for container in concat(var.scope.folders, var.scope.organizations) : can(regex("^[0-9]{1,20}$", container))
    ])
    error_message = "Each scope.folders and scope.organizations entry is a numeric Resource Manager ID (^[0-9]{1,20}$), the pattern the CRD accepts for the same fields; folders/<id> and organizations/<id> prefixes are not accepted."
  }

  validation {
    condition = alltrue([
      for project in var.scope.projects : can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", project))
    ])
    error_message = "Each scope.projects entry must be a GCP project ID (^[a-z][a-z0-9-]{4,28}[a-z0-9]$), the pattern the CRD accepts for the same field."
  }

  validation {
    condition = alltrue([
      for selector in concat(var.scope.shared_vpc_hosts, var.scope.metrics_scopes) : can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", selector))
    ])
    error_message = "Each scope.shared_vpc_hosts and scope.metrics_scopes entry is a GCP project ID (^[a-z][a-z0-9-]{4,28}[a-z0-9]$), the pattern the CRD accepts for the same fields: the Shared VPC host project, or the scoping project of the Metrics Scope."
  }

  validation {
    condition = alltrue([
      for cluster in var.scope.exclude.clusters :
      can(regex("^[a-z0-9][a-z0-9-]{0,62}$", cluster.project_id))
      && can(regex("^[a-z0-9][a-z0-9-]{0,62}$", cluster.location))
      && can(regex("^[a-z0-9][a-z0-9-]{0,62}$", cluster.cluster_name))
    ])
    error_message = "Each scope.exclude.clusters entry names one cluster by project_id, location and cluster_name, each matching ^[a-z0-9][a-z0-9-]*$ and at most 63 characters, as the CRD requires."
  }

  validation {
    condition = alltrue([
      for entry in var.scope.exclude.projects : can(regex("^[a-z0-9*?\\[\\]!-]{1,63}$", entry))
    ])
    error_message = "Each scope.exclude.projects entry is a project ID or a shell-style glob (lowercase letters, digits, - * ? [ ] !, up to 63 characters), the pattern the CRD accepts for the same field."
  }

  # The CRD declares the two project lists as sets and the cluster list as a
  # map keyed on the triple, so a repeated entry that Terraform let through
  # would bind IAM and then fail the CR at admission, after the apply.
  validation {
    condition = (
      length(distinct(var.scope.projects)) == length(var.scope.projects)
      && length(distinct(var.scope.folders)) == length(var.scope.folders)
      && length(distinct(var.scope.organizations)) == length(var.scope.organizations)
      && length(distinct(var.scope.shared_vpc_hosts)) == length(var.scope.shared_vpc_hosts)
      && length(distinct(var.scope.metrics_scopes)) == length(var.scope.metrics_scopes)
      && length(distinct(var.scope.exclude.projects)) == length(var.scope.exclude.projects)
      && length(distinct([for c in var.scope.exclude.clusters : "${c.project_id}/${c.location}/${c.cluster_name}"])) == length(var.scope.exclude.clusters)
    )
    error_message = "scope.projects, scope.folders, scope.organizations, scope.shared_vpc_hosts, scope.metrics_scopes, scope.exclude.projects and scope.exclude.clusters each name an entry once; the CRD rejects a repeat at admission, after IAM has been applied."
  }
}

variable "scope_container_members" {
  description = <<-EOT
    What each of scope.folders and scope.organizations held at plan time
    while the scoped service account pool is armed: the
    kube-agents-scope-resolver module's `container_members` output, a map
    from the container's key (folders/<id>, organizations/<id>) to the
    project IDs with a GKE cluster beneath it, as its Asset Inventory search
    listed them. Feeds the pool alone (scoped_pool.tf): a member gets an
    account and nothing else, because the container-level grant is inherited
    and containers are not counted toward the resolved-set cap. With the pool
    armed, every declared container needs an entry (an empty list for one
    with no clusters), or the plan is refused (main.tf); with it off the
    resolver reads no container and this stays empty. A key for a container
    the scope does not declare adds nothing. Resolved outside this module for
    the reason scope_selector_members is (scope.tf).
  EOT
  type        = map(list(string))
  nullable    = false
  default     = {}

  validation {
    condition = alltrue([
      for name, members in var.scope_container_members :
      can(regex("^(folders|organizations)/[0-9]+$", name))
      && alltrue([for member in members : can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", member))])
    ])
    error_message = "Each scope_container_members key is folders/<numeric id> or organizations/<numeric id>, and each member a GCP project ID (^[a-z][a-z0-9-]{4,28}[a-z0-9]$): the resolver module's container_members output as it is."
  }
}

variable "scope_selector_members" {
  description = <<-EOT
    What each of scope.shared_vpc_hosts and scope.metrics_scopes resolved to
    at plan time: the kube-agents-scope-resolver module's `members` output, a
    map from the selector's snapshot name (sharedVpcHosts/<host>,
    metricsScopes/<scope>) to the project IDs it reaches. Every declared
    selector needs an entry, or the plan is refused (main.tf); a key for a
    selector the scope does not declare binds nothing. Resolved outside this
    module because the composition calls it with a module-level depends_on,
    which would defer a data source here to apply time (scope.tf).
  EOT
  type        = map(list(string))
  nullable    = false
  default     = {}

  validation {
    condition = alltrue([
      for name, members in var.scope_selector_members :
      can(regex("^(sharedVpcHosts|metricsScopes)/[a-z][a-z0-9-]{4,28}[a-z0-9]$", name))
      && alltrue([for member in members : can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", member))])
    ])
    error_message = "Each scope_selector_members key is sharedVpcHosts/<project id> or metricsScopes/<project id>, and each member a GCP project ID (^[a-z][a-z0-9-]{4,28}[a-z0-9]$): the resolver module's members output as it is."
  }
}
