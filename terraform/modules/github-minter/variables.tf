variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "location" {
  description = "GCP region for the KMS key ring"
  type        = string
}

variable "service_account_id" {
  description = "IAM Service Account ID for the GitHub token minter. Fixed per project, so a second install in the same project must set its own. Passing null selects this default (nullable = false), which lets root modules expose a passthrough variable."
  type        = string
  nullable    = false
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

variable "owner_labels" {
  description = "Labels naming the install that owns the signing key, set on the crypto key (a KMS key ring carries no labels, so the ring is owned through its key). The installer's adoption step reads them to refuse another install's key. Empty sets none."
  type        = map(string)
  default     = {}
}

variable "owner_description" {
  description = "Description of the minter's service account, naming the install that owns it, for the same check. Null leaves the description unset."
  type        = string
  default     = null
}
