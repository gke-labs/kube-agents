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
    existing cluster must already have Workload Identity enabled, and
    enabling CMEK database encryption on it stays a
    `gcloud container clusters update --database-encryption-key` step outside
    Terraform — a data source cannot mutate the cluster.
  EOT
  type        = bool
  default     = true
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

variable "standard_machine_type" {
  description = "Machine type for the Standard cluster's default node pool. Only used when cluster_mode = \"standard\"."
  type        = string
  default     = "e2-standard-4"
}

variable "standard_node_count" {
  description = "Node count per zone for the Standard cluster's default node pool. Only used when cluster_mode = \"standard\"."
  type        = number
  default     = 1
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
