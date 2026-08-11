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
    Project-level IAM roles granted to the agent's service account. The default
    is the read-only permission set the provisioning scripts grant
    (k8s-operator/scripts/provision_04_gcp_iam.sh, PLATFORM_AGENT_PERMISSION_SET
    read-only, which is also that script's default); see the security-and-iam
    reference for what each role is used for. Set to [] to grant nothing and
    manage roles outside the module. Passing null selects this default
    (nullable = false), which lets root modules expose a passthrough variable.
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
