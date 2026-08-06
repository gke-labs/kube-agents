variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "location" {
  description = "GCP region for the KMS key ring"
  type        = string
}

variable "service_account_id" {
  description = "IAM Service Account ID for the GitHub token minter"
  type        = string
  default     = "kubeagents-github-minter-gsa"

  validation {
    condition     = can(regex("^[a-z]([-a-z0-9]{4,28}[a-z0-9])$", var.service_account_id))
    error_message = "service_account_id must be 6-30 characters, start with a lowercase letter, and contain only lowercase letters, digits, and hyphens."
  }
}

variable "namespace" {
  description = "Kubernetes namespace where the GitHub token minter runs"
  type        = string
  default     = "kubeagents-system"
}

variable "ksa_name" {
  description = "Kubernetes Service Account name of the GitHub token minter"
  type        = string
  default     = "kubeagents-github-minter"
}

variable "kms_keyring_name" {
  description = "Name of the KMS key ring holding the GitHub App signing key"
  type        = string
  default     = "github-token-minter-keyring"
}

variable "kms_key_name" {
  description = "Name of the KMS asymmetric signing key the GitHub App private key is imported into"
  type        = string
  default     = "github-token-minter-key"
}
