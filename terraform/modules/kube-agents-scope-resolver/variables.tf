variable "shared_vpc_hosts" {
  description = <<-EOT
    Shared VPC host project IDs (`spec.scope.sharedVpcHosts`). Each is
    resolved to the service projects attached to it, by ID, through the
    Compute API's getXpnResources; a project that is not a Shared VPC host
    resolves to no members, as it does at runtime. The host itself is not a
    member.
  EOT
  type        = list(string)
  nullable    = false
  default     = []

  validation {
    condition     = alltrue([for entry in var.shared_vpc_hosts : can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", entry))])
    error_message = "Each shared_vpc_hosts entry is a GCP project ID (^[a-z][a-z0-9-]{4,28}[a-z0-9]$), the pattern the CRD accepts for the same field."
  }
}

variable "metrics_scopes" {
  description = <<-EOT
    Cloud Monitoring Metrics Scope scoping-project IDs
    (`spec.scope.metricsScopes`). Each is resolved to the projects it
    monitors, the scoping project included, through the Monitoring API's
    metricsScopes.get, which names them by project number; each number is
    then named through Resource Manager.
  EOT
  type        = list(string)
  nullable    = false
  default     = []

  validation {
    condition     = alltrue([for entry in var.metrics_scopes : can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", entry))])
    error_message = "Each metrics_scopes entry is a GCP project ID (^[a-z][a-z0-9-]{4,28}[a-z0-9]$), the pattern the CRD accepts for the same field."
  }
}

variable "folders" {
  description = <<-EOT
    Resource Manager folder IDs (`spec.scope.folders`), numeric. Read only
    while `list_container_members` is set: each is then listed to the projects
    its GKE clusters are in, through one Cloud Asset Inventory
    searchAllResources call, the search the reconcile makes each run, for the
    scoped service account pool alone. The folder's own grant is inherited and
    is kube-agents-iam's; a member listed here gets a pool account and
    nothing else.
  EOT
  type        = list(string)
  nullable    = false
  default     = []

  validation {
    condition     = alltrue([for entry in var.folders : can(regex("^[0-9]+$", entry))])
    error_message = "Each folders entry is a numeric Resource Manager folder ID (^[0-9]+$), as spec.scope.folders names one."
  }
}

variable "organizations" {
  description = <<-EOT
    Resource Manager organization IDs (`spec.scope.organizations`), numeric,
    read as `folders` are and only while `list_container_members` is set.
  EOT
  type        = list(string)
  nullable    = false
  default     = []

  validation {
    condition     = alltrue([for entry in var.organizations : can(regex("^[0-9]+$", entry))])
    error_message = "Each organizations entry is a numeric Resource Manager organization ID (^[0-9]+$), as spec.scope.organizations names one."
  }
}

variable "list_container_members" {
  description = <<-EOT
    The scoped service account pool is armed (`scoped_pool_enabled`), so the
    declared folders' and organizations' members are listed at plan time for
    it, one Cloud Asset Inventory search per container, and the identity that
    plans needs roles/cloudasset.viewer on each container and
    cloudasset.googleapis.com enabled in quota_project. Off, no container is
    read and `container_members` is empty: the container-level grant and the
    reconcile's discovery stay zero-touch either way.
  EOT
  type        = bool
  nullable    = false
  default     = false
}

variable "quota_project" {
  description = <<-EOT
    The project every read is billed to and whose enabled APIs it uses,
    sent as the x-goog-user-project header: the management project. With
    it the answer does not depend on the credential's type (a user credential
    has no consumer project of its own; a service account's is its own
    project, which need not be the management project), and the APIs the
    reads need (cloudresourcemanager, monitoring, compute; cloudasset while
    containers are listed) are the ones the composition enables there, and
    install.sh enables before a first apply.
    The identity needs serviceusage.services.use on it, which an identity
    that applies the composition holds; a plan-only identity without it is
    refused with that grant named (USER_PROJECT_DENIED), not with the API's
    enable command.
  EOT
  type        = string
  nullable    = false

  # The installer's rule (is_valid_project_id), not the CRD's: a legacy
  # domain-scoped management project (example.com:name) is a project this
  # module is in the plan of whether or not a selector is declared, so the
  # CRD's pattern here would refuse every plan and destroy of such an install.
  validation {
    condition     = can(regex("^([a-z0-9][a-z0-9.-]*[a-z0-9]:)?[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.quota_project))
    error_message = "quota_project is a GCP project ID (^[a-z][a-z0-9-]{4,28}[a-z0-9]$, or a legacy domain-scoped domain:name)."
  }
}

variable "exclude_projects" {
  description = <<-EOT
    The scope's `exclude.projects` entries. Two kinds of entry act here. A
    bare project number: a monitored project the Monitoring API returned
    under that number is neither named nor listed, and the reconcile matches
    the number on every row a scope named by it, so the member leaves the set
    whether or not a run had named it before. An exact project ID naming a
    container's member: it is dropped from `container_members`, so it gets no
    pool account. For the selectors, IDs and globs are the caller's to
    apply (kube-agents-iam withholds the grant of a Shared VPC service project
    an entry names by ID and keeps a monitored project's, which the reconcile's
    naming call needs; the reconcile evaluates globs).
  EOT
  type        = list(string)
  nullable    = false
  default     = []
}

variable "member_cap" {
  description = <<-EOT
    The most projects one selector, or one listed container, may resolve to: the resolved-set cap the
    reconcile lists per run (spec.scope.maxProjects; kube-agents-iam's
    scope.max_projects), since a single selector past it cannot fit whatever
    else the scope declares, and refusing it at the read spares the naming
    reads the whole-set check would otherwise wait for. The reconcile's
    default, 100, when not given. A Shared VPC host is also bounded by one
    page of the Compute API's answer, 500 service projects, and a listed
    container by one page of the Asset Inventory search, 500 clusters,
    whatever this is.
  EOT
  type        = number
  nullable    = false
  default     = 100

  validation {
    condition     = var.member_cap >= 1 && var.member_cap <= 5000 && floor(var.member_cap) == var.member_cap
    error_message = "member_cap is a whole number from 1 to 5000, the bounds the CRD puts on spec.scope.maxProjects."
  }
}
