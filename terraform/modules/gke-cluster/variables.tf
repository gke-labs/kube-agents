variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "cluster_name" {
  description = "GKE cluster name"
  type        = string
}

variable "cluster_mode" {
  description = <<-EOT
    Which kind of cluster to manage. "autopilot" (the default) creates a GKE
    Autopilot cluster. "standard" creates a GKE Standard cluster: a default
    node pool of e2-standard-4 machines, Dataplane V2 with FQDN
    NetworkPolicy, the Filestore CSI and BackupRestore addons, Workload
    Identity, and (by default) CMEK database encryption.

    The managed OpenTelemetry collection scope
    (`--managed-otel-scope=COLLECTION_AND_INSTRUMENTATION_COMPONENTS`) has no
    Terraform field on either google provider. Callers that need it set it
    once after create with `gcloud container clusters update`; the provider
    does not know the field, so Terraform never sees or reverts it.
  EOT
  type        = string
  default     = "autopilot"

  validation {
    condition     = contains(["autopilot", "standard"], var.cluster_mode)
    error_message = "cluster_mode must be \"autopilot\" or \"standard\"."
  }
}

variable "create_cluster" {
  description = <<-EOT
    Whether the module creates the cluster. Set false to install onto a
    cluster somebody else made: the module then only reads the named cluster
    through a data source and creates no cluster or KMS resources. The
    existing cluster must already have Workload Identity enabled and enforce
    NetworkPolicy (Dataplane V2 or the legacy Calico addon); two postconditions
    refuse the plan otherwise, the second unless accept_no_network_policy is
    set. Enabling CMEK database encryption on it stays a
    `gcloud container clusters update --database-encryption-key` step outside
    Terraform — a data source cannot mutate the cluster.
  EOT
  type        = bool
  default     = true
}

variable "accept_no_network_policy" {
  description = <<-EOT
    Install onto an existing cluster (create_cluster = false) that enforces no
    NetworkPolicy — neither Dataplane V2 nor the legacy Calico addon — instead
    of refusing the plan. The cluster is not modified. Every NetworkPolicy
    kube-agents installs is then accepted by the API server and enforced by
    nothing: the agent pod's ingress and egress confinement, the shell
    sandbox's deny-all policy, and the LiteLLM, GitHub minter and Hindsight
    policies. What is lost is the confinement of
    kube-agents' own workloads, not of yours. The composition records the
    choice on the PlatformAgent as the
    kubeagents.x-k8s.io/network-policy-enforcement annotation. No effect on a
    cluster the module creates (Dataplane V2) or one that already enforces.
  EOT
  type        = bool
  default     = false
}

variable "location" {
  description = "GCP location for the cluster: a region (e.g. us-central1), or a zone (e.g. us-central1-a) for a zonal Standard or pre-existing cluster. Autopilot clusters are regional, so a zone is rejected at plan time in autopilot mode."
  type        = string

  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]+(-[a-z])?$", var.location))
    error_message = "location must be a region (e.g. us-central1) or a zone (e.g. us-central1-a)."
  }
}

variable "deletion_protection" {
  description = "Whether deletion protection is enabled on the cluster"
  type        = bool
  default     = true
}

variable "allow_external_dns_traffic" {
  description = "Whether the DNS-based control plane endpoint serves traffic from outside the VPC. The Platform Agent's endpoint detection reads this field, and without it a cluster the agent cannot route to over its IP endpoint is unreachable. Defaults to false — GKE's own default, and the value every cluster this module already manages is at — so that upgrading the module does not publish an endpoint on an existing cluster; set it true for a cluster the agent must reach from outside the VPC (install.sh's generated tfvars always set it true)."
  type        = bool
  default     = false
}

variable "resource_labels" {
  description = "GCP resource labels to apply to the cluster. Set kube-agents-host=true when the cluster hosts kube-agents. Ignored when create_cluster = false."
  type        = map(string)
  default     = {}
}

variable "release_channel" {
  description = "GKE release channel for the cluster"
  type        = string
  default     = "REGULAR"

  validation {
    # EXTENDED is deliberately not accepted: it is not supported for this
    # module's Autopilot clusters and would only fail later at plan/apply.
    condition     = contains(["RAPID", "REGULAR", "STABLE"], var.release_channel)
    error_message = "release_channel must be one of RAPID, REGULAR, or STABLE."
  }
}

variable "enable_database_encryption" {
  description = "Whether to enable Cloud KMS database encryption for GKE etcd secrets (CMEK). Ignored when create_cluster = false: encrypting an existing cluster is a gcloud update outside Terraform."
  type        = bool
  default     = true
}

variable "enable_fqdn_network_policy" {
  description = <<-EOT
    Whether to enable FQDN NetworkPolicy on the cluster. The
    operator's opt-in FQDNNetworkPolicy companion (the
    kubeagents.x-k8s.io/enable-fqdn-network-policy annotation) can only enforce
    on clusters where this is on.
  EOT
  type        = bool
  default     = true
}

variable "kms_keyring_name" {
  description = "Name of the Cloud KMS Keyring for GKE database encryption."
  type        = string
  default     = "platform-agent-keyring"
}

variable "kms_key_name" {
  description = "Name of the Cloud KMS CryptoKey for GKE database encryption."
  type        = string
  default     = "k8s-secret-encryption-key"
}

variable "enable_backup_agent" {
  description = <<-EOT
    Whether to enable the Backup for GKE agent (the BackupRestore addon) on the
    cluster. Defaults to true. Enabling the agent
    costs nothing on its own — backups are only taken once a BackupPlan targets
    the cluster (terraform/modules/gke-backup-plan). Requires
    gkebackup.googleapis.com to be enabled on the project.
  EOT
  type        = bool
  default     = true
}

variable "sizing_profile" {
  description = <<-EOT
    Which shape the Standard cluster's two node pools take when a machine type
    or disk size is not given explicitly: "small" (the default), "medium", or
    "fleet". "small" resolves to e2-standard-4 with a 100 GB pd-balanced boot
    disk on both pools, today's rendering; "medium" resolves like "small"
    until a later change gives it its own shape; "fleet" resolves the default
    pool to n2-standard-8 with a 200 GB disk and the gVisor pool to
    n2-standard-4 with a 150 GB disk. An explicit standard_* or gvisor_*
    machine type or disk value always wins over the profile. The resolved
    values are the module's standard_* and gvisor_* outputs. Only used when
    cluster_mode = "standard".
  EOT
  type        = string
  default     = "small"

  validation {
    condition     = contains(["small", "medium", "fleet"], var.sizing_profile)
    error_message = "sizing_profile must be \"small\", \"medium\" or \"fleet\"."
  }
}

variable "standard_machine_type" {
  description = "Machine type for the Standard cluster's default node pool. Null, the default, takes the sizing_profile's machine type (e2-standard-4 under \"small\"); an explicit value wins over the profile. Only used when cluster_mode = \"standard\"."
  type        = string
  default     = null
}

variable "standard_node_count" {
  description = "Node count per zone the Standard cluster's default node pool is created with. It is google_container_cluster's initial_node_count, which the google provider treats as ForceNew on the cluster itself: changing it on a live cluster plans the cluster's destruction and recreation, so resize a live default pool with gcloud instead. Only used when cluster_mode = \"standard\"."
  type        = number
  default     = 1
}

variable "standard_disk_size_gb" {
  description = "Boot disk size in GB for the Standard cluster's default node pool, at least 10. Null, the default, takes the sizing_profile's size (100 under \"small\"); an explicit value wins over the profile. Only used when cluster_mode = \"standard\"."
  type        = number
  default     = null

  # A conditional rather than `== null || ...`: Terraform below 1.10 evaluates
  # both operands of ||, and `null >= 10` fails the plan at the default.
  validation {
    condition     = var.standard_disk_size_gb == null ? true : var.standard_disk_size_gb >= 10
    error_message = "standard_disk_size_gb must be at least 10 GB, GKE's floor for a boot disk."
  }
}

variable "standard_disk_type" {
  description = "Boot disk type for the Standard cluster's default node pool: pd-standard, pd-balanced, pd-ssd or hyperdisk-balanced. Null, the default, takes the sizing_profile's type (pd-balanced under every profile); an explicit value wins over the profile. Only used when cluster_mode = \"standard\"."
  type        = string
  default     = null

  # "" counts as unset, as coalesce already treats it for the machine types:
  # a blanked TF_VAR_ line in install.env exports "". Equality tests only, so
  # Terraform below 1.10, which evaluates both operands, plans at null too.
  validation {
    condition     = (var.standard_disk_type == null || var.standard_disk_type == "") ? true : contains(["pd-standard", "pd-balanced", "pd-ssd", "hyperdisk-balanced"], var.standard_disk_type)
    error_message = "standard_disk_type must be pd-standard, pd-balanced, pd-ssd or hyperdisk-balanced."
  }
}

variable "enable_gvisor_node_pool" {
  description = <<-EOT
    Whether to add a dedicated GKE Sandbox (gVisor) node pool.
    Standard mode only: on Autopilot the gvisor RuntimeClass is available
    without a node pool, so requesting the pool there fails at plan time
    rather than silently doing nothing. Works with create_cluster = false —
    the pool attaches to the named existing Standard cluster, so gVisor can
    be added to a running cluster.
  EOT
  type        = bool
  default     = false
}

variable "gvisor_pool_name" {
  description = "Name of the gVisor node pool."
  type        = string
  default     = "gvisor-pool"
}

variable "gvisor_machine_type" {
  description = "Machine type for the gVisor node pool. Null, the default, takes the sizing_profile's machine type (e2-standard-4 under \"small\"); an explicit value wins over the profile. Only used when enable_gvisor_node_pool = true."
  type        = string
  default     = null
}

variable "gvisor_min_node_count" {
  description = "Lower autoscaling bound of the gVisor node pool, per zone (a regional location scales each zone within these bounds), and the node count the pool is created with. Raising it later moves the bound in place; the creation count is left as it was. Only used when enable_gvisor_node_pool = true."
  type        = number
  default     = 1

  validation {
    condition     = var.gvisor_min_node_count >= 1 && floor(var.gvisor_min_node_count) == var.gvisor_min_node_count
    error_message = "gvisor_min_node_count must be a whole number of at least 1."
  }
}

variable "gvisor_max_node_count" {
  description = "Upper autoscaling bound of the gVisor node pool, per zone. Must be at least gvisor_min_node_count; equal to it keeps the pool at a fixed size. Only used when enable_gvisor_node_pool = true."
  type        = number
  default     = 2

  validation {
    condition     = var.gvisor_max_node_count >= 1 && floor(var.gvisor_max_node_count) == var.gvisor_max_node_count
    error_message = "gvisor_max_node_count must be a whole number of at least 1."
  }
}

variable "gvisor_disk_size_gb" {
  description = "Boot disk size in GB for the gVisor node pool, at least 10. Null, the default, takes the sizing_profile's size (100 under \"small\"); an explicit value wins over the profile. Only used when enable_gvisor_node_pool = true."
  type        = number
  default     = null

  validation {
    condition     = var.gvisor_disk_size_gb == null ? true : var.gvisor_disk_size_gb >= 10
    error_message = "gvisor_disk_size_gb must be at least 10 GB, GKE's floor for a boot disk."
  }
}

variable "gvisor_disk_type" {
  description = "Boot disk type for the gVisor node pool: pd-standard, pd-balanced, pd-ssd or hyperdisk-balanced. Null, the default, takes the sizing_profile's type (pd-balanced under every profile); an explicit value wins over the profile. Only used when enable_gvisor_node_pool = true."
  type        = string
  default     = null

  # "" counts as unset, as coalesce already treats it for the machine types:
  # a blanked TF_VAR_ line in install.env exports "". Equality tests only, so
  # Terraform below 1.10, which evaluates both operands, plans at null too.
  validation {
    condition     = (var.gvisor_disk_type == null || var.gvisor_disk_type == "") ? true : contains(["pd-standard", "pd-balanced", "pd-ssd", "hyperdisk-balanced"], var.gvisor_disk_type)
    error_message = "gvisor_disk_type must be pd-standard, pd-balanced, pd-ssd or hyperdisk-balanced."
  }
}

variable "create_after" {
  description = "An opaque value the cluster is ordered after. Terraform destroys dependents before dependencies, so a caller that passes something derived from another component gets that component torn down AFTER this cluster -- which is why full-install passes the drift audit ingress: the control plane emits audit records until it is gone, and a topic deleted while it is still emitting mails every project owner. The value itself is never read. Pass null, the default, for no ordering. This is an input rather than a depends_on on the module because a module-level depends_on defers every data source inside it, including the one that reads an existing cluster when create_cluster is false; that leaves the helm provider without a host at plan time, and a helm provider that cannot reach the cluster plans every release as a create."
  type        = any
  default     = null
}
