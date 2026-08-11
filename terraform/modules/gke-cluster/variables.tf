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
