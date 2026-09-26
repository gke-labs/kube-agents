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

    The default was going to depend on scoped_clusters: with a pool the
    per-cluster accounts would carry roles/container.viewer and the agent would
    drop it, keeping roles/container.clusterViewer -- enough to enumerate the
    fleet and run `get-credentials`, not enough to read anything inside a
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
    "roles/iam.serviceAccountUser",
    "roles/iam.securityReviewer",
    "roles/mcp.toolUser",
    "roles/serviceusage.serviceUsageConsumer",
  ]
}

variable "scoped_clusters" {
  description = <<-EOT
    GKE clusters to provision a reader service account for -- one account per
    cluster. Empty (the default) provisions no pool and leaves the agent's
    single wide identity in place, which is the pre-existing behaviour.

    As of 2026-08-12 these accounts hold no IAM grant. They were scoped by an
    IAM Condition on the cluster's resource.name; that grants nothing for
    Kubernetes object operations, and un-conditioned the same binding is
    project-wide container.viewer. Both are gone. Authority arrives with
    per-cluster RBAC -- see scoped_pool.tf -- and until then the broker runs on
    the ambient credential by default.

    Cardinality is per (project, location, cluster) and not per scope tier.
    project_id is per entry rather than inherited so that a cluster in another
    project is a row in this list rather than a second module.

    Every cluster the agent is expected to read must appear here. One that does
    not is refused by the broker rather than served by a wider credential, which
    is intended -- but it means this list and the live fleet are two things that
    can drift, and the drift shows up as a refusal.
  EOT
  type = list(object({
    project_id   = string
    location     = string
    cluster_name = string
  }))
  nullable = false
  default  = []

  validation {
    condition = alltrue([
      for cluster in var.scoped_clusters :
      can(regex("^[a-z0-9][a-z0-9-]*$", cluster.project_id))
      && can(regex("^[a-z0-9][a-z0-9-]*$", cluster.location))
      && can(regex("^[a-z0-9][a-z0-9-]*$", cluster.cluster_name))
    ])
    error_message = "Each of project_id, location and cluster_name must match ^[a-z0-9][a-z0-9-]*$. The values are interpolated into the key the credential broker matches on, so a separator or a quote in one of them would produce a key that silently matches nothing."
  }

  validation {
    condition = length(distinct([
      for cluster in var.scoped_clusters :
      "${cluster.project_id}/${cluster.location}/${cluster.cluster_name}"
    ])) == length(var.scoped_clusters)
    error_message = "scoped_clusters repeats a cluster. One cluster maps to one service account; two entries would silently keep whichever the provider applied last."
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

    Empty, the default, binds nothing and the reconcile lists project_id alone.
    Folders and organisations are not inputs yet; they arrive with the
    container half of the design.
  EOT
  type = object({
    projects = optional(list(string), [])
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

  validation {
    condition = (
      length(var.scope.projects) <= 100
      && length(var.scope.exclude.projects) <= 100
      && length(var.scope.exclude.clusters) <= 100
    )
    error_message = "scope.projects, scope.exclude.projects and scope.exclude.clusters each carry at most 100 entries, the cap the CRD enforces on the same lists."
  }

  validation {
    condition = alltrue([
      for project in var.scope.projects : can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", project))
    ])
    error_message = "Each scope.projects entry must be a GCP project ID (^[a-z][a-z0-9-]{4,28}[a-z0-9]$), the pattern the CRD accepts for the same field."
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
      && length(distinct(var.scope.exclude.projects)) == length(var.scope.exclude.projects)
      && length(distinct([for c in var.scope.exclude.clusters : "${c.project_id}/${c.location}/${c.cluster_name}"])) == length(var.scope.exclude.clusters)
    )
    error_message = "scope.projects, scope.exclude.projects and scope.exclude.clusters each name an entry once; the CRD rejects a repeat at admission, after IAM has been applied."
  }
}
