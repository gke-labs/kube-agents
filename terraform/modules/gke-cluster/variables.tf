variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "cluster_name" {
  description = "GKE Autopilot cluster name"
  type        = string
}

variable "location" {
  description = "GCP region for the cluster. Autopilot clusters are regional, so a zone (e.g. us-central1-a) is rejected."
  type        = string

  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]+$", var.location))
    error_message = "location must be a region (e.g. us-central1); GKE Autopilot does not support zonal clusters."
  }
}

variable "deletion_protection" {
  description = "Whether deletion protection is enabled on the cluster"
  type        = bool
  default     = true
}

variable "resource_labels" {
  description = "GCP resource labels to apply to the cluster. Set kube-agents-host=true when the cluster hosts kube-agents."
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
  description = "Whether to enable Cloud KMS database encryption for GKE etcd secrets (CMEK)."
  type        = bool
  default     = true
}

variable "enable_fqdn_network_policy" {
  description = <<-EOT
    Whether to enable FQDN NetworkPolicy on the cluster, matching the
    --enable-fqdn-network-policy flag provision_01_gcp_cluster.sh passes. The
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
    cluster. Defaults to true, matching the cluster
    k8s-operator/scripts/provision_01_gcp_cluster.sh creates. Enabling the agent
    costs nothing on its own — backups are only taken once a BackupPlan targets
    the cluster (terraform/modules/gke-backup-plan). Requires
    gkebackup.googleapis.com to be enabled on the project.
  EOT
  type        = bool
  default     = true
}
