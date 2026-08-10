variable "project_id" {
  description = "GCP Project ID hosting the audit logs, the topic, and the subscription"
  type        = string
}

variable "detector_service_account_email" {
  description = "Email of the Google Service Account the drift detector runs as (granted subscriber and viewer on the subscription). The GSA itself and its Workload Identity binding belong to the kube-agents-iam module, not to this one."
  type        = string

  validation {
    condition     = can(regex(".+@.+\\.iam\\.gserviceaccount\\.com$", var.detector_service_account_email))
    error_message = "detector_service_account_email must be a service account email (name@project.iam.gserviceaccount.com)."
  }
}

variable "cluster_names" {
  description = "GKE clusters to export audit logs for. Empty (the default) exports every cluster in the project, leaving the detector to route on resource.labels.cluster_name."
  type        = list(string)
  default     = []
}

variable "topic_name" {
  description = "Pub/Sub topic the Log Router sink publishes audit entries to"
  type        = string
  default     = "platform-agent-drift-audit"
}

variable "subscription_name" {
  description = "Pub/Sub subscription the drift detector pulls audit entries from"
  type        = string
  default     = "platform-agent-drift-audit-sub"
}

variable "sink_name" {
  description = "Name of the Log Router sink exporting GKE audit logs to the topic"
  type        = string
  default     = "platform-agent-drift-audit-sink"
}

variable "ack_deadline_seconds" {
  description = "How long the detector has to ack a message before Pub/Sub redelivers it. The detector acks on successful parse, so this only needs to cover parsing, not the managedFields join or the inject."
  type        = number
  default     = 60

  validation {
    condition     = var.ack_deadline_seconds >= 10 && var.ack_deadline_seconds <= 600
    error_message = "ack_deadline_seconds must be between 10 and 600."
  }
}

variable "message_retention_duration" {
  description = "How long unacked messages are retained, as a duration string. The default of 7 days matches the window the Phase 0 spike measured its noise profile over."
  type        = string
  default     = "604800s"
}

variable "retry_minimum_backoff" {
  description = "Lower bound of the exponential backoff applied to redelivery after a nack"
  type        = string
  default     = "10s"
}

variable "retry_maximum_backoff" {
  description = "Upper bound of the exponential backoff applied to redelivery after a nack"
  type        = string
  default     = "600s"
}
