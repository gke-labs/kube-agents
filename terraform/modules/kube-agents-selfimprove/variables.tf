variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace the self-improvement CronJob runs in"
  type        = string
  default     = "kubeagents-system"
}

variable "service_account_id" {
  description = "IAM service account for the investigator. Must match selfImprovement.github.gsaName in the chart."
  type        = string
  default     = "kubeagents-selfimprove"

  validation {
    # 30 is GCP's own cap on a service account id, and it is now the only one:
    # no account derives from this name by suffix. Checked here as well as in
    # the chart because either can be applied without the other, and an id GCP
    # rejects fails at apply while an id the chart rejects fails at render.
    condition     = can(regex("^[a-z]([-a-z0-9]{4,28}[a-z0-9])$", var.service_account_id))
    error_message = "service_account_id must be 6-30 characters, start with a lowercase letter, and contain only lowercase letters, digits, and hyphens."
  }
}

variable "ksa_name" {
  description = "Kubernetes service account the CronJob runs as. Must match selfImprovement.github.ksaName."
  type        = string
  default     = "kubeagents-selfimprove"

  validation {
    # An RFC 1123 subdomain, which is what Kubernetes accepts for a
    # ServiceAccount name. This value is interpolated into a Workload Identity
    # member string, where a name Kubernetes would reject still applies cleanly
    # and binds nothing -- the failure surfaces later as a CronJob that cannot
    # authenticate, with no error naming this variable.
    condition     = can(regex("^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$", var.ksa_name)) && length(var.ksa_name) <= 253
    error_message = "ksa_name must be a valid RFC 1123 subdomain: lowercase letters, digits, '-' and '.', starting and ending alphanumeric, at most 253 characters."
  }
}
