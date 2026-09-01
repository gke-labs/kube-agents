variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "service_account_id" {
  description = "IAM Service Account ID for Kube-Agents"
  type        = string
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
    Project-level IAM roles granted to the agent's service account, and the only
    list the module binds -- `local.agent_project_roles` in main.tf reads this
    and nothing else. The default below mirrors `read_only_roles` in
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
