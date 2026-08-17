variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "cluster_name" {
  description = "Name of the GKE cluster the plan backs up"
  type        = string
}

variable "location" {
  description = "Region of both the cluster and the BackupPlan"
  type        = string
}

variable "name" {
  description = "Name of the BackupPlan. Null derives <cluster_name>-backup-plan, the name provision_12_gke_backup_plan.sh uses."
  type        = string
  default     = null
}

variable "selected_namespaces" {
  description = "Namespaces included in each backup. Defaults to the namespace kube-agents installs into."
  type        = list(string)
  default     = ["kubeagents-system"]

  validation {
    condition     = length(var.selected_namespaces) > 0
    error_message = "selected_namespaces must name at least one namespace; an empty list would back up nothing."
  }
}

variable "cron_schedule" {
  description = "Cron schedule for automatic backups (5 fields, cluster-local time)"
  type        = string
  default     = "0 2 * * *"

  validation {
    condition     = length(split(" ", trimspace(var.cron_schedule))) == 5
    error_message = "cron_schedule must be a 5-field cron expression, e.g. '0 2 * * *'."
  }
}

variable "backup_retain_days" {
  description = "How many days each backup is retained before it is deleted"
  type        = number
  default     = 30

  validation {
    condition     = var.backup_retain_days > 0 && floor(var.backup_retain_days) == var.backup_retain_days
    error_message = "backup_retain_days must be a positive whole number of days."
  }
}

variable "paused" {
  description = "Whether the schedule is paused. The provisioning script always un-pauses the plan it reconciles."
  type        = bool
  default     = false
}

variable "include_secrets" {
  description = "Whether backups include Kubernetes Secrets. True mirrors the provisioning script — the agent's credentials Secret is otherwise lost on restore. Restrict backup/restore IAM accordingly."
  type        = bool
  default     = true
}

variable "include_volume_data" {
  description = "Whether backups include persistent volume data (mirrors the provisioning script's --include-volume-data)"
  type        = bool
  default     = true
}

variable "encryption_key" {
  description = "Optional Cloud KMS CryptoKey resource path encrypting the backups. Empty uses Google-managed encryption."
  type        = string
  default     = ""

  validation {
    condition     = var.encryption_key == "" || can(regex("^projects/[^/]+/locations/[^/]+/keyRings/[^/]+/cryptoKeys/[^/]+$", var.encryption_key))
    error_message = "encryption_key must be empty or a full Cloud KMS CryptoKey path (projects/P/locations/L/keyRings/R/cryptoKeys/K)."
  }
}
