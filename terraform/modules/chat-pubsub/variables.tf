variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "topic_name" {
  description = "Pub/Sub topic Google Chat publishes inbound events to"
  type        = string
  default     = "platform-agent-chat-events"
}

variable "subscription_name" {
  description = "Pub/Sub subscription the Platform Agent pulls chat events from"
  type        = string
  default     = "platform-agent-chat-events-sub"
}

variable "agent_service_account_email" {
  description = "Email of the Platform Agent's Google Service Account (granted subscriber and viewer on the subscription)"
  type        = string

  validation {
    condition     = can(regex(".+@.+\\.iam\\.gserviceaccount\\.com$", var.agent_service_account_email))
    error_message = "agent_service_account_email must be a service account email (name@project.iam.gserviceaccount.com)."
  }
}
